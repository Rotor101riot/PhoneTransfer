"""
extract_photos_ios.py

Extracts photos and videos from an iOS device's DCIM folder and returns a
list of MediaFile objects defined in normalization_schema.py.

Strategy
--------
Uses the standard AFCConnector (no jailbreak required) since
/var/mobile/Media/DCIM is accessible via the standard AFC share.

The AFC root is /var/mobile/Media, so DCIM inside the AFC service appears
as "/DCIM" (no leading /var/mobile/Media prefix).

Walk "/DCIM" recursively, pull each media file to staging_dir/photos/,
optionally read EXIF metadata from JPEGs via Pillow.

Never raises — all exceptions are caught, logged, and return partial/empty
results.
"""

from __future__ import annotations

import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)

# Map file extensions to MIME types
_MIME_MAP: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".webp": "image/webp",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".3gp": "video/3gpp",
    ".avi": "video/x-msvideo",
}

# AFC-relative path for DCIM (AFC root = /var/mobile/Media)
_DCIM_AFC_PATH = "/DCIM"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(udid: str, staging_dir: Path, is_jailbroken: bool = False) -> list[MediaFile]:
    """
    Extract all photos/videos from the iOS device identified by *udid*.

    Parameters
    ----------
    udid:           iOS device UDID.
    staging_dir:    Local directory used for temporary file copies.
    is_jailbroken:  Unused — standard AFC always works for DCIM.

    Returns
    -------
    list[MediaFile]   Possibly empty on total failure.
    """
    try:
        return _extract_impl(udid, staging_dir)
    except Exception as exc:
        logger.exception("extract_photos_ios: top-level failure for %s: %s", udid, exc)
        return []


def _extract_impl(udid: str, staging_dir: Path) -> list[MediaFile]:
    photos_dir = staging_dir / "photos_ios"
    photos_dir.mkdir(parents=True, exist_ok=True)

    afc = _get_afc_connector(udid)
    if afc is None:
        logger.warning("photos_ios: could not open AFC for %s — no photos extracted", udid)
        return []

    media_files: list[MediaFile] = []
    _walk_dcim(afc, _DCIM_AFC_PATH, photos_dir, media_files)
    logger.info("photos_ios: extracted %d media files for %s", len(media_files), udid)
    return media_files


# ---------------------------------------------------------------------------
# AFC connector
# ---------------------------------------------------------------------------

def _get_afc_connector(udid: str):
    """Return an AFCConnector for the device, or None on failure."""
    try:
        from core.device_connection_cache import get_broker
        from core.afc_connector import AFCConnector

        broker = get_broker(udid)
        return AFCConnector(broker)
    except Exception as exc:
        logger.error("photos_ios: failed to open AFC for %s: %s", udid, exc)
        return None


# ---------------------------------------------------------------------------
# Recursive DCIM walk
# ---------------------------------------------------------------------------

def _walk_dcim(
    afc,
    afc_path: str,
    local_root: Path,
    result: list[MediaFile],
    _depth: int = 0,
) -> None:
    """
    Recursively list *afc_path* via AFC and pull every media file found.
    Subdirectory results are appended to *result* in-place.
    """
    if _depth > 8:
        # Sanity guard against symlink cycles or unexpectedly deep trees
        return

    try:
        entries = afc.list_dir(afc_path)
    except Exception as exc:
        logger.debug("photos_ios: list_dir(%s) failed: %s", afc_path, exc)
        return

    for entry in entries:
        if entry in (".", ".."):
            continue

        child_afc = afc_path.rstrip("/") + "/" + entry

        # Determine whether this entry is a directory by trying to list it
        if _is_afc_dir(afc, child_afc):
            _walk_dcim(afc, child_afc, local_root, result, _depth + 1)
        else:
            media = _pull_media_file(afc, child_afc, local_root)
            if media is not None:
                result.append(media)


def _is_afc_dir(afc, afc_path: str) -> bool:
    """
    Return True if *afc_path* is a directory on the device.
    Uses stat info (st_ifmt == 'S_IFDIR') when available, otherwise
    heuristically tries list_dir.
    """
    try:
        info = afc.stat(afc_path)
        if info and isinstance(info, dict):
            ifmt = info.get("st_ifmt", "")
            return ifmt == "S_IFDIR"
    except Exception:
        pass

    # Fallback: try listing — returns non-empty list for dirs
    try:
        entries = afc.list_dir(afc_path)
        return entries is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Pull a single media file
# ---------------------------------------------------------------------------

def _pull_media_file(afc, afc_path: str, local_root: Path) -> MediaFile | None:
    """
    Pull one file from AFC to local_root, preserving the relative sub-path
    from the DCIM directory.  Returns a MediaFile or None if skipped/failed.
    """
    suffix = PurePosixPath(afc_path).suffix.lower()
    mime = _mime_for_extension(suffix)
    if mime is None:
        # Not a recognised media file — skip silently
        return None

    # Relative path below DCIM (e.g. "100APPLE/IMG_0001.JPG")
    rel_parts = PurePosixPath(afc_path).parts
    # Drop the leading /DCIM component(s) to get the relative sub-path
    try:
        dcim_idx = next(
            i for i, p in enumerate(rel_parts) if p.upper() == "DCIM"
        )
        rel_sub = "/".join(rel_parts[dcim_idx + 1:])
    except StopIteration:
        rel_sub = PurePosixPath(afc_path).name

    local_path = local_root / Path(rel_sub)
    local_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        ok = afc.pull_file(afc_path, local_path)
        if not ok or not local_path.exists():
            logger.debug("photos_ios: pull_file failed for %s", afc_path)
            return None
    except Exception as exc:
        logger.debug("photos_ios: pull_file(%s) exception: %s", afc_path, exc)
        return None

    # Determine album from immediate parent folder name
    album = PurePosixPath(rel_sub).parent.name or None
    if album in (".", "DCIM", ""):
        album = None

    # Try to read EXIF metadata
    created, latitude, longitude = _read_exif(local_path, mime)

    return MediaFile(
        filename=local_path.name,
        mime_type=mime,
        local_path=local_path,
        created=created,
        album=album,
        latitude=latitude,
        longitude=longitude,
    )


# ---------------------------------------------------------------------------
# MIME type helpers
# ---------------------------------------------------------------------------

def _mime_for_extension(suffix: str) -> str | None:
    """
    Return a MIME type string for the given file extension (with leading dot,
    lowercase), or None if it is not a recognised media type.
    """
    if not suffix:
        return None
    mime = _MIME_MAP.get(suffix)
    if mime:
        return mime
    # Try stdlib mimetypes as a last resort
    guessed, _ = mimetypes.guess_type(f"file{suffix}")
    if guessed and (guessed.startswith("image/") or guessed.startswith("video/")):
        return guessed
    return None


# ---------------------------------------------------------------------------
# EXIF extraction
# ---------------------------------------------------------------------------

def _read_exif(
    local_path: Path, mime: str
) -> tuple[datetime | None, float | None, float | None]:
    """
    Attempt to read EXIF data from a JPEG or PNG using Pillow.
    Returns (created, latitude, longitude); any element may be None.
    Does not raise.
    """
    if mime not in ("image/jpeg", "image/png", "image/tiff"):
        return None, None, None

    try:
        from PIL import Image  # type: ignore[import]
        from PIL.ExifTags import TAGS, GPSTAGS

        with Image.open(local_path) as img:
            exif_data = img._getexif()  # type: ignore[attr-defined]
            if exif_data is None:
                return None, None, None

        tag_map = {TAGS.get(k, k): v for k, v in exif_data.items()}

        created: datetime | None = None
        for dt_tag in ("DateTimeOriginal", "DateTime", "DateTimeDigitized"):
            raw = tag_map.get(dt_tag)
            if raw:
                try:
                    created = datetime.strptime(raw, "%Y:%m:%d %H:%M:%S").replace(
                        tzinfo=timezone.utc
                    )
                    break
                except ValueError:
                    pass

        # GPS
        latitude: float | None = None
        longitude: float | None = None
        gps_info_raw = tag_map.get("GPSInfo")
        if gps_info_raw and isinstance(gps_info_raw, dict):
            gps = {GPSTAGS.get(k, k): v for k, v in gps_info_raw.items()}
            try:
                latitude = _dms_to_decimal(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
                longitude = _dms_to_decimal(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
            except Exception:
                pass

        return created, latitude, longitude

    except ImportError:
        logger.debug("photos_ios: Pillow not installed — skipping EXIF for %s", local_path.name)
        return None, None, None
    except Exception as exc:
        logger.debug("photos_ios: EXIF read failed for %s: %s", local_path.name, exc)
        return None, None, None


def _dms_to_decimal(dms, ref) -> float | None:
    """Convert DMS (degrees, minutes, seconds) tuple to decimal degrees."""
    if dms is None or len(dms) < 3:
        return None
    try:
        d = float(dms[0])
        m = float(dms[1])
        s = float(dms[2])
        decimal = d + m / 60.0 + s / 3600.0
        if ref in ("S", "W"):
            decimal = -decimal
        return decimal
    except (TypeError, ValueError, ZeroDivisionError):
        return None
