from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from core.config_loader import get_config
from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)

_STAGING_SUBDIR = "ringtones_android"

# MediaStore content URIs for ringtones
_MEDIASTORE_URIS = [
    "content://media/internal/audio/media",
    "content://media/external/audio/media",
]
_PROJECTION = "_id:_display_name:_data:mime_type:date_added"
_WHERE = "is_ringtone=1"

# Direct filesystem paths to also scan
_FS_SCAN_PATHS = ["/sdcard/Ringtones"]


def extract(device_id: str, staging_dir: Path, is_privileged: bool) -> list:
    """Extract ringtones from an Android device via ADB.

    Queries MediaStore content providers for ringtone-flagged audio files and
    also scans /sdcard/Ringtones directly.  Pulls each file to staging_dir.

    Args:
        device_id: ADB serial of the Android device.
        staging_dir: Root staging directory; files are saved under ringtones_android/.
        is_privileged: True if the device is rooted (not required for this module).

    Returns:
        list[MediaFile] of extracted ringtones, or [] on failure.
    """
    cfg = get_config()
    out_dir = staging_dir / _STAGING_SUBDIR
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("Failed to create staging directory %s", out_dir)
        return []

    adb = [str(cfg.adb_exe), "-s", device_id]
    seen_paths: set[str] = set()
    results: list[MediaFile] = []

    # --- MediaStore queries ---
    for uri in _MEDIASTORE_URIS:
        rows = _query_mediastore(adb, uri)
        for row in rows:
            data_path = row.get("_data")
            if not data_path or data_path in seen_paths:
                continue
            seen_paths.add(data_path)
            mf = _pull_file(adb, row, out_dir)
            if mf is not None:
                results.append(mf)

    # --- Direct filesystem scan of /sdcard/Ringtones ---
    for fs_path in _FS_SCAN_PATHS:
        found = _find_files(adb, fs_path)
        for remote_path in found:
            if remote_path in seen_paths:
                continue
            seen_paths.add(remote_path)
            mf = _pull_raw(adb, remote_path, out_dir)
            if mf is not None:
                results.append(mf)

    logger.info("Extracted %d Android ringtone(s)", len(results))
    return results


# ---------------------------------------------------------------------------
# MediaStore helpers
# ---------------------------------------------------------------------------

def _query_mediastore(adb: list[str], uri: str) -> list[dict[str, str]]:
    """Run a MediaStore content query and parse the output into row dicts."""
    cmd = adb + [
        "shell", "content", "query",
        "--uri", uri,
        "--where", _WHERE,
        "--projection", _PROJECTION,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return _parse_content_query(proc.stdout)
    except Exception:
        logger.warning("MediaStore query failed for URI %s", uri, exc_info=True)
        return []


def _parse_content_query(output: str) -> list[dict[str, str]]:
    """Parse `adb shell content query` output into a list of field dicts.

    Each output line looks like:
        Row: 0 _id=1, _display_name=MyRingtone.mp3, _data=/storage/..., ...
    """
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("Row:"):
            continue
        # Strip leading "Row: N "
        body = re.sub(r"^Row:\s*\d+\s*", "", line)
        row: dict[str, str] = {}
        # Split on ", " boundaries that precede a known key name
        for part in re.split(r",\s*(?=\w+=)", body):
            if "=" in part:
                key, _, value = part.partition("=")
                row[key.strip()] = value.strip()
        if row:
            rows.append(row)
    return rows


def _pull_file(adb: list[str], row: dict[str, str], out_dir: Path) -> MediaFile | None:
    """Pull a file described by a MediaStore row to out_dir."""
    remote_path = row.get("_data", "")
    display_name = row.get("_display_name", Path(remote_path).name)
    mime_type = row.get("mime_type", "audio/mpeg")
    date_added_raw = row.get("date_added")

    created = None
    if date_added_raw and date_added_raw.isdigit():
        import datetime
        created = datetime.datetime.fromtimestamp(int(date_added_raw))

    local_path = out_dir / display_name
    if not _adb_pull(adb, remote_path, local_path):
        return None

    return MediaFile(
        filename=display_name,
        mime_type=mime_type,
        local_path=local_path,
        created=created,
        album="ringtone",
        latitude=None,
        longitude=None,
    )


# ---------------------------------------------------------------------------
# Filesystem scan helpers
# ---------------------------------------------------------------------------

def _find_files(adb: list[str], remote_dir: str) -> list[str]:
    """Run `adb shell find <dir> -type f` and return a list of remote paths."""
    cmd = adb + ["shell", "find", remote_dir, "-type", "f"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return [p.strip() for p in proc.stdout.splitlines() if p.strip()]
    except Exception:
        logger.debug("find failed for %s", remote_dir, exc_info=True)
        return []


def _pull_raw(adb: list[str], remote_path: str, out_dir: Path) -> MediaFile | None:
    """Pull an arbitrary remote file without MediaStore metadata."""
    filename = Path(remote_path).name
    local_path = out_dir / filename
    if not _adb_pull(adb, remote_path, local_path):
        return None

    # Determine MIME type from extension
    ext = Path(filename).suffix.lower()
    mime_map = {".mp3": "audio/mpeg", ".m4r": "audio/x-m4r", ".ogg": "audio/ogg",
                ".aac": "audio/aac", ".wav": "audio/wav", ".flac": "audio/flac"}
    mime_type = mime_map.get(ext, "audio/mpeg")

    return MediaFile(
        filename=filename,
        mime_type=mime_type,
        local_path=local_path,
        created=None,
        album="ringtone",
        latitude=None,
        longitude=None,
    )


# ---------------------------------------------------------------------------
# Low-level ADB pull
# ---------------------------------------------------------------------------

def _adb_pull(adb: list[str], remote_path: str, local_path: Path) -> bool:
    cmd = adb + ["pull", remote_path, str(local_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            logger.warning("adb pull failed for %s: %s", remote_path, proc.stderr.strip())
            return False
        return True
    except Exception:
        logger.warning("adb pull raised for %s", remote_path, exc_info=True)
        return False
