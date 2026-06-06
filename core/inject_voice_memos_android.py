from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from core.config_loader import get_config
from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)

_REMOTE_RECORDINGS_DIR = "/sdcard/Recordings/PhoneTransfer"


def inject(device_id: str, items: list, staging_dir: Path, is_privileged: bool) -> int:
    """Inject voice memos onto an Android device via ADB.

    Pushes audio files to /sdcard/Recordings/PhoneTransfer/ and triggers a
    MediaStore scan so recording apps can discover them.

    Args:
        device_id: ADB serial of the Android device.
        items: List of MediaFile objects; only album="voice_memo" are processed.
        staging_dir: Staging root (unused but kept for signature consistency).
        is_privileged: True if device is rooted (not required here).

    Returns:
        Count of successfully pushed files.
    """
    cfg = get_config()
    adb = [str(cfg.adb_exe), "-s", device_id]

    memos = [mf for mf in items if isinstance(mf, MediaFile) and mf.album == "voice_memo"]
    if not memos:
        logger.info("No voice memo items to inject onto Android device")
        return 0

    # Ensure destination directory exists on device
    _adb_shell(adb, ["mkdir", "-p", _REMOTE_RECORDINGS_DIR])

    count = 0
    for mf in memos:
        src = mf.local_path
        if not src or not src.exists():
            logger.warning("Source file missing for voice memo %s", mf.filename)
            continue

        remote_path = f"{_REMOTE_RECORDINGS_DIR}/{mf.filename}"

        push_cmd = adb + ["push", str(src), remote_path]
        try:
            proc = subprocess.run(push_cmd, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                logger.warning(
                    "adb push failed for %s: %s", src, proc.stderr.strip()
                )
                continue
        except Exception:
            logger.warning("adb push raised for %s", src, exc_info=True)
            continue

        _trigger_media_scan(adb, remote_path)
        logger.debug("Pushed voice memo %s -> %s", src, remote_path)
        count += 1

    if count:
        logger.info(
            "Pushed %d voice memo(s) to %s on device.", count, _REMOTE_RECORDINGS_DIR
        )
    return count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trigger_media_scan(adb: list[str], remote_path: str) -> None:
    """Broadcast MEDIA_SCANNER_SCAN_FILE so the file appears in MediaStore."""
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
