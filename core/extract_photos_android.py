"""
extract_photos_android.py

Extracts photos, videos, and other media files from an Android device
connected via ADB.

Strategy:
1. Primary: use `adb shell find` across known media directories
   (/sdcard/DCIM, /sdcard/Pictures, /sdcard/Movies) to enumerate files,
   then `adb pull` each file to staging.
2. Supplementary: query the MediaStore content provider to obtain GPS
   coordinates, date_taken, and album metadata where the filesystem
   alone cannot provide them.

No root is required for reading from /sdcard/.  The is_rooted flag is
accepted for API consistency but not used — /sdcard/ is always accessible.

Returns a list of MediaFile objects as defined in normalization_schema.py.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from core.adb_manager import ADBManager
from core.config_loader import get_config
from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Directories searched on the device
# ---------------------------------------------------------------------------

_SEARCH_DIRS = [
    "/sdcard/DCIM",
    "/sdcard/Pictures",
    "/sdcard/Movies",
]

# Default file extensions (case-insensitive).  Used when no filter is active.
_EXTENSIONS = [
    "*.jpg",
    "*.jpeg",
    "*.png",
    "*.mp4",
    "*.mov",
    "*.heic",
    "*.gif",
    "*.webp",
    "*.3gp",
    "*.mkv",
]

# Frozenset of the same extensions with a leading dot — used for local
# filtering after tar extraction.
_DEFAULT_EXTENSIONS_SET: frozenset[str] = frozenset(
    "." + e.lstrip("*.") for e in _EXTENSIONS
)

# MediaStore content provider URI for images
_URI_IMAGES = "content://media/external/images/media"
_URI_VIDEO = "content://media/external/video/media"

# Staging sub-directory
_SUBDIR = "photos_android"

# Maximum number of files to pull in one session (safety cap)
_MAX_FILES = 50_000


# ---------------------------------------------------------------------------
# MIME type helpers
# ---------------------------------------------------------------------------

# Ensure common mobile types are registered
_EXTRA_MIME: dict[str, str] = {
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".3gp": "video/3gpp",
    ".mkv": "video/x-matroska",
    ".webp": "image/webp",
}


def _mime_for_path(path: str) -> str:
    """Return a MIME type string for the given file path / extension."""
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _EXTRA_MIME:
        return _EXTRA_MIME[suffix]
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


# ---------------------------------------------------------------------------
# Content row parser
# ---------------------------------------------------------------------------

def _parse_content_rows(output: str) -> list[dict[str, str]]:
    """
    Parse the stdout of `adb shell content query` into a list of dicts.

    Handles values that contain commas by splitting only at ", key=" boundaries.
    """
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("Row:"):
            continue
        _, _, rest = line.partition(" ")   # drop "Row:"
        _, _, rest = rest.partition(" ")   # drop row index
        rest = rest.strip()
        if not rest:
            continue
        pairs = re.split(r',\s+(?=\w+=)', rest)
        row: dict[str, str] = {}
        for pair in pairs:
            k, _, v = pair.partition("=")
            row[k.strip()] = v.strip()
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(
    serial: str,
    staging_dir: Path,
    is_rooted: bool = False,
) -> list[MediaFile]:
    """
    Extract media files from the Android device.

    Parameters
    ----------
    serial:      ADB device serial string.
    staging_dir: Root staging directory for this transfer session.
    is_rooted:   Accepted for API consistency; /sdcard/ is readable without
                 root so this flag does not change behaviour here.

    Returns
    -------
    List of MediaFile objects; empty list on any fatal error.
    """
    try:
        sub = staging_dir / _SUBDIR
        sub.mkdir(parents=True, exist_ok=True)

        cfg = get_config()
        adb = ADBManager(cfg)

        # Determine the active extension filter (set by FileFilterDialog, or
        # None to use the built-in default set).
        ext_filter: frozenset[str] | None = None
        if cfg.storage_filter_extensions is not None:
            ext_filter = frozenset(cfg.storage_filter_extensions)

        # Build supplementary metadata map from MediaStore
        meta_map = _query_mediastore_metadata(serial, adb)

        # Enumerate files from the filesystem (applies extension filter to the
        # find command so we never enumerate files we won't pull).
        remote_paths = _find_media_files(serial, adb, ext_filter=ext_filter)
        if not remote_paths:
            logger.info("[photos/android] No media files found on device")
            return []

        logger.info(
            "[photos/android] Found %d media files to pull", len(remote_paths)
        )

        # ── Try tar fast path ──────────────────────────────────────────────
        # Tar pulls an entire directory tree in one ADB round-trip, which is
        # dramatically faster than individual adb pull calls for large libraries.
        from core.tar_transfer import probe_tar, pull_dirs_with_tar

        results: list[MediaFile] = []
        tar_used = False

        if probe_tar(serial, adb):
            logger.info("[photos/android] tar available — using bulk tar transfer")
            extracted_files, tar_ok = pull_dirs_with_tar(
                serial, _SEARCH_DIRS, sub, adb, timeout=600
            )
            if tar_ok:
                tar_used = True
                active_exts = ext_filter if ext_filter is not None else _DEFAULT_EXTENSIONS_SET
                results = _build_from_tar_extract(
                    extracted_files, active_exts, meta_map
                )
                logger.info(
                    "[photos/android] tar path: built %d MediaFile objects",
                    len(results),
                )

        if not tar_used:
            # ── Fallback: individual adb pull ──────────────────────────────
            if tar_used is False and probe_tar(serial, adb) is False:
                logger.info(
                    "[photos/android] tar not available — using individual adb pull"
                )
            else:
                logger.warning(
                    "[photos/android] tar transfer failed — falling back to "
                    "individual adb pull"
                )
            for remote_path in remote_paths[:_MAX_FILES]:
                media_file = _pull_and_build(
                    serial, remote_path, sub, adb, meta_map
                )
                if media_file is not None:
                    results.append(media_file)

        logger.info(
            "[photos/android] Successfully staged %d media files", len(results)
        )
        return results

    except Exception:
        logger.exception("[photos/android] Unhandled error during extraction")
        return []


# ---------------------------------------------------------------------------
# File enumeration
# ---------------------------------------------------------------------------

def _find_media_files(
    serial: str,
    adb: ADBManager,
    ext_filter: "frozenset[str] | None" = None,
) -> list[str]:
    """
    Run ``adb shell find`` across all known media directories and return a
    list of absolute remote paths to matching files.

    Parameters
    ----------
    ext_filter:
        Optional set of lowercase extensions with leading dot (e.g.
        ``{'.jpg', '.mp4'}``).  When provided, only those extensions are
        passed to the ``find`` command.  When None the default ``_EXTENSIONS``
        glob list is used.
    """
    if ext_filter is not None:
        # Convert frozenset of ".jpg" style extensions to "*.jpg" glob patterns
        globs = [f"*{ext}" for ext in sorted(ext_filter)]
    else:
        globs = _EXTENSIONS

    iname_parts = " -o ".join(f'-iname "{g}"' for g in globs)
    dirs_str = " ".join(_SEARCH_DIRS)
    cmd = f"find {dirs_str} -type f \\( {iname_parts} \\) 2>/dev/null"

    stdout, stderr, rc = adb.shell(serial, cmd, timeout=120)
    if rc != 0 and not stdout.strip():
        logger.warning(
            "[photos/android] find command failed (rc=%d): %s", rc, stderr
        )
        return []

    paths: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line and not line.startswith("find:"):
            paths.append(line)

    return paths


def _build_from_tar_extract(
    extracted_files: list[Path],
    active_exts: "frozenset[str]",
    meta_map: dict[str, dict],
) -> list[MediaFile]:
    """
    Build MediaFile objects from a list of locally extracted files.

    Called after a successful ``pull_dirs_with_tar()`` to convert the raw
    file list into the normalised schema.

    Parameters
    ----------
    extracted_files:
        List of local Path objects produced by the tar extraction.
    active_exts:
        Set of lowercase extensions (e.g. ``{'.jpg', '.mp4'}``) used to
        filter out any non-media files that happened to sit inside the tarred
        directories.
    meta_map:
        MediaStore metadata dict keyed by display name (from
        ``_query_mediastore_metadata``).
    """
    results: list[MediaFile] = []
    seen: set[str] = set()  # de-duplicate by filename within extraction

    for local_path in sorted(extracted_files):
        suffix = local_path.suffix.lower()
        if suffix not in active_exts:
            continue  # skip non-media files that were inside the directories

        filename = local_path.name

        # Guard against files with identical names in different directories
        unique_name = filename
        if unique_name in seen:
            stem = local_path.stem
            counter = 1
            while unique_name in seen:
                unique_name = f"{stem}_{counter}{suffix}"
                counter += 1
        seen.add(unique_name)

        mime = _mime_for_path(filename)

        # Album from the immediate parent directory
        album = local_path.parent.name or ""

        # Metadata from MediaStore (keyed by original filename)
        row_meta = meta_map.get(filename, {})

        # Timestamp
        created: datetime | None = None
        date_taken = row_meta.get("date_taken", "")
        if date_taken and date_taken not in ("0", "null", ""):
            try:
                ts = int(date_taken) / 1000.0
                created = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, TypeError):
                pass
        if created is None:
            try:
                mtime = local_path.stat().st_mtime
                created = datetime.fromtimestamp(mtime, tz=timezone.utc)
            except OSError:
                pass

        # GPS from MediaStore
        latitude: float | None = None
        longitude: float | None = None
        try:
            lat_str = row_meta.get("latitude", "")
            lon_str = row_meta.get("longitude", "")
            if lat_str and lat_str not in ("0.0", "null", ""):
                latitude = float(lat_str)
            if lon_str and lon_str not in ("0.0", "null", ""):
                longitude = float(lon_str)
        except (ValueError, TypeError):
            pass

        if row_meta.get("album"):
            album = row_meta["album"]

        results.append(
            MediaFile(
                filename=unique_name,
                mime_type=mime,
                local_path=local_path,
                created=created,
                album=album or None,
                latitude=latitude,
                longitude=longitude,
            )
        )

    return results


# ---------------------------------------------------------------------------
# MediaStore metadata query
# ---------------------------------------------------------------------------

def _query_mediastore_metadata(
    serial: str,
    adb: ADBManager,
) -> dict[str, dict]:
    """
    Query the Android MediaStore for GPS coordinates, date_taken, and
    album names.  Returns a dict keyed by the file's display name.
    Falls back gracefully on failure.
    """
    meta: dict[str, dict] = {}
    for uri in (_URI_IMAGES, _URI_VIDEO):
        stdout, _, rc = adb.shell(
            serial,
            (
                "content query "
                f"--uri {uri} "
                "--projection _display_name,date_taken,latitude,longitude,"
                "bucket_display_name"
            ),
            timeout=60,
        )
        if rc != 0:
            continue
        for row in _parse_content_rows(stdout):
            name = row.get("_display_name", "").strip()
            if not name:
                continue
            meta[name] = {
                "date_taken": row.get("date_taken", ""),
                "latitude": row.get("latitude", ""),
                "longitude": row.get("longitude", ""),
                "album": row.get("bucket_display_name", ""),
            }
    return meta


# ---------------------------------------------------------------------------
# Per-file pull and MediaFile construction
# ---------------------------------------------------------------------------

def _pull_and_build(
    serial: str,
    remote_path: str,
    sub: Path,
    adb: ADBManager,
    meta_map: dict[str, dict],
) -> MediaFile | None:
    """
    Pull a single remote file to *sub* and build a MediaFile for it.
    Returns None if the pull fails.
    """
    posix = PurePosixPath(remote_path)
    filename = posix.name
    local_path = _unique_local_path(sub, filename)

    pulled = adb.pull_verified(serial, remote_path, local_path, timeout=120)
    if not pulled or not local_path.exists():
        logger.warning("[photos/android] Failed to pull or size mismatch: %s", remote_path)
        return None

    mime = _mime_for_path(filename)

    # Derive album from the parent directory name
    album = _album_from_remote_path(remote_path)

    # Timestamp: try MediaStore first, then `adb shell stat`
    created = _get_timestamp(serial, remote_path, filename, adb, meta_map)

    # GPS from MediaStore
    latitude: float | None = None
    longitude: float | None = None
    row_meta = meta_map.get(filename, {})
    try:
        lat_str = row_meta.get("latitude", "")
        lon_str = row_meta.get("longitude", "")
        if lat_str and lat_str not in ("0.0", "null", ""):
            latitude = float(lat_str)
        if lon_str and lon_str not in ("0.0", "null", ""):
            longitude = float(lon_str)
    except (ValueError, TypeError):
        pass

    # Override album from MediaStore if present
    if row_meta.get("album"):
        album = row_meta["album"]

    return MediaFile(
        filename=local_path.name,
        mime_type=mime,
        local_path=local_path,
        created=created,
        album=album or None,
        latitude=latitude,
        longitude=longitude,
    )


def _get_timestamp(
    serial: str,
    remote_path: str,
    filename: str,
    adb: ADBManager,
    meta_map: dict[str, dict],
) -> datetime | None:
    """
    Attempt to determine the file creation timestamp.

    Priority:
    1. MediaStore date_taken (milliseconds since epoch)
    2. `adb shell stat -c %Y` (seconds since epoch)
    """
    # 1. MediaStore date_taken
    row_meta = meta_map.get(filename, {})
    date_taken = row_meta.get("date_taken", "")
    if date_taken and date_taken not in ("0", "null", ""):
        try:
            ts = int(date_taken) / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, TypeError):
            pass

    # 2. stat on device
    stdout, _, rc = adb.shell(
        serial,
        f"stat -c %Y {_shell_quote(remote_path)}",
        timeout=15,
    )
    if rc == 0:
        try:
            ts = int(stdout.strip())
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, TypeError):
            pass

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _album_from_remote_path(remote_path: str) -> str:
    """
    Derive a human-readable album name from the file's directory path.

    Examples:
        /sdcard/DCIM/Camera/IMG_001.jpg        -> "Camera"
        /sdcard/Pictures/Screenshots/shot.png  -> "Screenshots"
        /sdcard/DCIM/IMG_001.jpg               -> "DCIM"
    """
    posix = PurePosixPath(remote_path)
    parent_name = posix.parent.name
    return parent_name if parent_name else ""


def _unique_local_path(directory: Path, filename: str) -> Path:
    """
    Return a Path inside *directory* for *filename* that does not already
    exist.  If *filename* is taken, appends _1, _2, ... before the suffix.
    """
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem = PurePosixPath(filename).stem
    suffix = PurePosixPath(filename).suffix
    counter = 1
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _shell_quote(path: str) -> str:
    """Wrap a remote path in single quotes, escaping any embedded quotes."""
    escaped = path.replace("'", "'\\''")
    return f"'{escaped}'"
