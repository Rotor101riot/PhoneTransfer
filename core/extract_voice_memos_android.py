from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from core.config_loader import get_config
from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)

_STAGING_SUBDIR = "voicememos_android"

# Common manufacturer-specific recording directories to scan
_FS_SCAN_DIRS = [
    "/sdcard/Recordings",
    "/sdcard/VoiceRecorder",
    "/sdcard/SoundRecorder",
    "/sdcard/Voice Recorder",
    "/sdcard/Music/Recordings",
    "/sdcard/MIUI/sound_recorder",   # Xiaomi
    "/sdcard/AudioRecorder",         # Samsung
    "/sdcard/Voice",
]

# Audio extensions to collect during filesystem scan
_AUDIO_EXTS = {".m4a", ".aac", ".mp3", ".amr", ".3gp", ".ogg", ".wav"}

# MIME map for raw filesystem files
_MIME_MAP = {
    ".m4a": "audio/m4a",
    ".aac": "audio/aac",
    ".mp3": "audio/mpeg",
    ".amr": "audio/amr",
    ".3gp": "audio/3gpp",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
}


def extract(device_id: str, staging_dir: Path, is_privileged: bool) -> list:
    """Extract voice recordings from an Android device via ADB.

    Combines two strategies:
      1. MediaStore query — finds audio not tagged as music/ringtone/alarm/notification/podcast.
      2. Direct filesystem scan of common recording directories.

    Args:
        device_id: ADB serial of the Android device.
        staging_dir: Root staging directory; files saved under voicememos_android/.
        is_privileged: True if device is rooted (not required here).

    Returns:
        list[MediaFile] with album="voice_memo", or [] on failure.
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

    # --- MediaStore query ---
    for row in _query_non_music_audio(adb):
        data_path = row.get("_data", "")
        if not data_path or data_path in seen_paths:
            continue
        seen_paths.add(data_path)
        mf = _pull_mediastore_row(adb, row, out_dir)
        if mf is not None:
            results.append(mf)

    # --- Filesystem scan ---
    found_paths = _find_audio_files(adb, _FS_SCAN_DIRS)
    for remote_path in found_paths:
        if remote_path in seen_paths:
            continue
        seen_paths.add(remote_path)
        mf = _pull_raw(adb, remote_path, out_dir)
        if mf is not None:
            results.append(mf)

    logger.info("Extracted %d Android voice memo(s)", len(results))
    return results


# ---------------------------------------------------------------------------
# MediaStore helpers
# ---------------------------------------------------------------------------

def _query_non_music_audio(adb: list[str]) -> list[dict[str, str]]:
    """Query MediaStore for audio not categorised as music/ringtone/alarm/notification/podcast."""
    projection = "_id:_display_name:_data:mime_type:date_added"
    where = "is_music=0 AND is_ringtone=0 AND is_alarm=0 AND is_notification=0 AND is_podcast=0"
    cmd = adb + [
        "shell", "content", "query",
        "--uri", "content://media/external/audio/media",
        "--where", where,
        "--projection", projection,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return _parse_content_query(proc.stdout)
    except Exception:
        logger.warning("MediaStore voice memo query failed", exc_info=True)
        return []


def _parse_content_query(output: str) -> list[dict[str, str]]:
    """Parse `adb shell content query` output into row dicts."""
    import re
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("Row:"):
            continue
        body = re.sub(r"^Row:\s*\d+\s*", "", line)
        row: dict[str, str] = {}
        for part in re.split(r",\s*(?=\w+=)", body):
            if "=" in part:
                key, _, value = part.partition("=")
                row[key.strip()] = value.strip()
        if row:
            rows.append(row)
    return rows


def _pull_mediastore_row(adb: list[str], row: dict[str, str], out_dir: Path) -> MediaFile | None:
    remote_path = row.get("_data", "")
    display_name = row.get("_display_name", Path(remote_path).name)
    mime_type = row.get("mime_type", "audio/m4a")
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
        album="voice_memo",
        latitude=None,
        longitude=None,
    )


# ---------------------------------------------------------------------------
# Filesystem scan helpers
# ---------------------------------------------------------------------------

def _find_audio_files(adb: list[str], dirs: list[str]) -> list[str]:
    """Scan multiple directories for audio files via `adb shell find`."""
    if not dirs:
        return []

    # Build a single find command covering all directories
    name_clauses: list[str] = []
    for ext in _AUDIO_EXTS:
        name_clauses += ["-o", "-name", f"*{ext}"]
    # Remove leading "-o"
    if name_clauses and name_clauses[0] == "-o":
        name_clauses = name_clauses[1:]

    cmd = adb + ["shell", "find"] + dirs + ["-type", "f", "("] + name_clauses + [")", "2>/dev/null"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return [p.strip() for p in proc.stdout.splitlines() if p.strip()]
    except Exception:
        logger.debug("find command failed", exc_info=True)
        return []


def _pull_raw(adb: list[str], remote_path: str, out_dir: Path) -> MediaFile | None:
    filename = Path(remote_path).name
    local_path = out_dir / filename
    if not _adb_pull(adb, remote_path, local_path):
        return None

    ext = Path(filename).suffix.lower()
    mime_type = _MIME_MAP.get(ext, "audio/m4a")

    return MediaFile(
        filename=filename,
        mime_type=mime_type,
        local_path=local_path,
        created=None,
        album="voice_memo",
        latitude=None,
        longitude=None,
    )


# ---------------------------------------------------------------------------
# Low-level ADB pull
# ---------------------------------------------------------------------------

def _adb_pull(adb: list[str], remote_path: str, local_path: Path) -> bool:
    cmd = adb + ["pull", remote_path, str(local_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            logger.warning("adb pull failed for %s: %s", remote_path, proc.stderr.strip())
            return False
        return True
    except Exception:
        logger.warning("adb pull raised for %s", remote_path, exc_info=True)
        return False
