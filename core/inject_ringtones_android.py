from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from core.config_loader import get_config
from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)

_REMOTE_RINGTONE_DIR = "/sdcard/Ringtones"


def inject(device_id: str, items: list, staging_dir: Path, is_privileged: bool) -> int:
    """Inject ringtones onto an Android device via ADB.

    For each ringtone MediaFile:
      1. Converts .m4r -> .mp3 via ffmpeg if necessary.
      2. Pushes the file to /sdcard/Ringtones/<filename>.
      3. Triggers a MediaStore scan so the file becomes system-visible.

    Args:
        device_id: ADB serial of the Android device.
        items: List of MediaFile objects; only album="ringtone" are processed.
        staging_dir: Staging root (used for conversion temp files).
        is_privileged: True if the device is rooted (not required here).

    Returns:
        Count of successfully pushed files.
    """
    cfg = get_config()
    adb = [str(cfg.adb_exe), "-s", device_id]

    ringtones = [mf for mf in items if isinstance(mf, MediaFile) and mf.album == "ringtone"]
    if not ringtones:
        logger.info("No ringtone items to inject onto Android device")
        return 0

    # Ensure destination directory exists on device
    _adb_shell(adb, ["mkdir", "-p", _REMOTE_RINGTONE_DIR])

    count = 0
    for mf in ringtones:
        local_path = _ensure_mp3(mf, staging_dir, cfg)
        if local_path is None:
            continue

        remote_path = f"{_REMOTE_RINGTONE_DIR}/{local_path.name}"

        # Push
        push_cmd = adb + ["push", str(local_path), remote_path]
        try:
            proc = subprocess.run(push_cmd, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                logger.warning(
                    "adb push failed for %s: %s", local_path, proc.stderr.strip()
                )
                continue
        except Exception:
            logger.warning("adb push raised for %s", local_path, exc_info=True)
            continue

        # MediaStore scan
        _trigger_media_scan(adb, remote_path)

        logger.debug("Pushed ringtone %s -> %s", local_path, remote_path)
        count += 1

    if count:
        logger.info(
            "Pushed %d ringtone(s) to %s. "
            "Use the device's Sound settings to set a file as the active ringtone.",
            count, _REMOTE_RINGTONE_DIR,
        )
    return count


# ---------------------------------------------------------------------------
# Format conversion helper
# ---------------------------------------------------------------------------

def _ensure_mp3(mf: MediaFile, staging_dir: Path, cfg) -> Path | None:
    """Return a .mp3 path, converting from .m4r via ffmpeg if required."""
    src = mf.local_path
    if not src or not src.exists():
        logger.warning("Source file missing for ringtone %s", mf.filename)
        return None

    if src.suffix.lower() == ".mp3":
        return src

    conv_dir = staging_dir / "ringtones_converted_android"
    try:
        conv_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("Could not create conversion dir %s", conv_dir)
        return None

    dest = conv_dir / (src.stem + ".mp3")

    ffmpeg_exe = getattr(cfg, "ffmpeg_exe", "ffmpeg")
    cmd = [
        str(ffmpeg_exe), "-y", "-i", str(src),
        "-c:a", "libmp3lame", "-q:a", "2",
        str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            logger.warning("ffmpeg conversion failed for %s: %s", src, proc.stderr[-500:])
            return None
        logger.debug("Converted %s -> %s", src, dest)
        return dest
    except Exception:
        logger.exception("ffmpeg conversion raised for %s", src)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trigger_media_scan(adb: list[str], remote_path: str) -> None:
    """Broadcast MEDIA_SCANNER_SCAN_FILE so the ringtone appears in MediaStore."""
    uri = f"file://{remote_path}"
    cmd = adb + [
        "shell", "am", "broadcast",
        "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
        "-d", uri,
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception:
        logger.debug("Media scan broadcast failed for %s", remote_path, exc_info=True)


def _adb_shell(adb: list[str], shell_args: list[str]) -> None:
    try:
        subprocess.run(adb + ["shell"] + shell_args, capture_output=True, text=True, timeout=15)
    except Exception:
        logger.debug("adb shell %s failed", shell_args, exc_info=True)
