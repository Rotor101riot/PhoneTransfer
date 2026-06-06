from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from core.config_loader import get_config
from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)

_STAGING_SUBDIR = "wallpaper_android"

# Path of the system wallpaper file (requires root)
_SYSTEM_WALLPAPER = "/data/system/users/0/wallpaper"
# Temp staging path on the device sdcard used for the pull
_DEVICE_TMP_PATH = "/sdcard/PhoneTransfer_wallpaper_tmp.jpg"


def extract(device_id: str, staging_dir: Path, is_privileged: bool) -> list:
    """Extract the current wallpaper from an Android device via ADB.

    Root is required to read /data/system/users/0/wallpaper.  On non-rooted
    devices this limitation is logged and an empty list is returned.

    Args:
        device_id: ADB serial of the Android device.
        staging_dir: Root staging directory; files saved under wallpaper_android/.
        is_privileged: True if device is rooted.

    Returns:
        list containing a single MediaFile for the wallpaper, or [] on failure.
    """
    if not is_privileged:
        logger.info(
            "Wallpaper extraction from Android requires root access. "
            "The system wallpaper at /data/system/users/0/wallpaper is not "
            "readable without root.  Skipping."
        )
        return []

    cfg = get_config()
    out_dir = staging_dir / _STAGING_SUBDIR
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("Failed to create staging directory %s", out_dir)
        return []

    adb = [str(cfg.adb_exe), "-s", device_id]
    return _extract_rooted(adb, out_dir)


# ---------------------------------------------------------------------------
# Rooted extraction
# ---------------------------------------------------------------------------

def _extract_rooted(adb: list[str], out_dir: Path) -> list:
    """Copy the wallpaper to sdcard via su, then pull it."""
    # Copy system wallpaper to a world-readable location via su
    cp_cmd = adb + [
        "shell", "su", "-c",
        f"cp {_SYSTEM_WALLPAPER} {_DEVICE_TMP_PATH} && chmod 644 {_DEVICE_TMP_PATH}",
    ]
    try:
        proc = subprocess.run(cp_cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            logger.warning(
                "Root copy of wallpaper failed: %s", proc.stderr.strip()
            )
            return []
    except Exception:
        logger.exception("Root copy command raised")
        return []

    # Pull from sdcard to host
    local_path = out_dir / "wallpaper.jpg"
    pull_cmd = adb + ["pull", _DEVICE_TMP_PATH, str(local_path)]
    try:
        proc = subprocess.run(pull_cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            logger.warning("adb pull of wallpaper failed: %s", proc.stderr.strip())
            return []
    except Exception:
        logger.exception("adb pull of wallpaper raised")
        return []

    # Clean up temp file on device
    _adb_shell(adb, ["rm", "-f", _DEVICE_TMP_PATH])

    if not local_path.exists() or local_path.stat().st_size == 0:
        logger.warning("Pulled wallpaper file is empty or missing at %s", local_path)
        return []

    logger.info("Extracted Android wallpaper to %s", local_path)
    return [
        MediaFile(
            filename="wallpaper.jpg",
            mime_type="image/jpeg",
            local_path=local_path,
            created=None,
            album="wallpaper_home",
            latitude=None,
            longitude=None,
        )
    ]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _adb_shell(adb: list[str], shell_args: list[str]) -> None:
    try:
        subprocess.run(adb + ["shell"] + shell_args, capture_output=True, text=True, timeout=15)
    except Exception:
        logger.debug("adb shell %s failed", shell_args, exc_info=True)
