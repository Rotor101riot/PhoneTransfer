from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from core.config_loader import get_config
from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)

_REMOTE_STAGING_DIR = "/sdcard/PhoneTransfer"
_REMOTE_WALLPAPER_PATH = "/sdcard/PhoneTransfer/wallpaper.jpg"

# System wallpaper path (rooted only)
_SYSTEM_WALLPAPER = "/data/system/users/0/wallpaper"


def inject(device_id: str, items: list, staging_dir: Path, is_privileged: bool) -> int:
    """Inject a wallpaper image onto an Android device via ADB.

    Non-rooted: The image is pushed to /sdcard/PhoneTransfer/wallpaper.jpg and
    the system 'cmd wallpaper set-wallpaper' command (Android 13+) is attempted.
    If that command is unavailable the user is instructed to set the wallpaper
    manually from the Gallery app.

    Rooted: The image is additionally copied directly to
    /data/system/users/0/wallpaper with correct permissions, and the launcher
    is restarted.

    Args:
        device_id: ADB serial of the Android device.
        items: List of MediaFile objects; files with album starting "wallpaper"
               are processed (first match wins).
        staging_dir: Staging root (unused here).
        is_privileged: True if device is rooted.

    Returns:
        1 if the file was pushed successfully, 0 otherwise.
    """
    cfg = get_config()
    adb = [str(cfg.adb_exe), "-s", device_id]

    wallpapers = [
        mf for mf in items
        if isinstance(mf, MediaFile) and str(mf.album).startswith("wallpaper")
    ]
    if not wallpapers:
        logger.info("No wallpaper items to inject onto Android device")
        return 0

    # Use the first wallpaper item found
    mf = wallpapers[0]
    src = mf.local_path
    if not src or not src.exists():
        logger.warning("Source file missing for wallpaper %s", mf.filename)
        return 0

    # Ensure staging directory exists on device
    _adb_shell(adb, ["mkdir", "-p", _REMOTE_STAGING_DIR])

    # Push image to sdcard
    push_cmd = adb + ["push", str(src), _REMOTE_WALLPAPER_PATH]
    try:
        proc = subprocess.run(push_cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            logger.warning("adb push of wallpaper failed: %s", proc.stderr.strip())
            return 0
    except Exception:
        logger.exception("adb push raised for wallpaper")
        return 0

    logger.debug("Pushed wallpaper to %s", _REMOTE_WALLPAPER_PATH)

    if is_privileged:
        _set_wallpaper_rooted(adb)
    else:
        _set_wallpaper_non_rooted(adb)

    return 1


# ---------------------------------------------------------------------------
# Rooted path
# ---------------------------------------------------------------------------

def _set_wallpaper_rooted(adb: list[str]) -> None:
    """Overwrite the system wallpaper file directly and restart the launcher."""
    cp_cmd = adb + [
        "shell", "su", "-c",
        f"cp {_REMOTE_WALLPAPER_PATH} {_SYSTEM_WALLPAPER} && chmod 600 {_SYSTEM_WALLPAPER}",
    ]
    try:
        proc = subprocess.run(cp_cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            logger.info("Copied wallpaper to %s via root", _SYSTEM_WALLPAPER)
        else:
            logger.warning("Root copy failed: %s", proc.stderr.strip())
    except Exception:
        logger.warning("Root copy raised", exc_info=True)

    # Restart launcher so the new wallpaper is picked up
    restart_cmd = adb + ["shell", "am", "force-stop", "com.android.launcher3"]
    try:
        subprocess.run(restart_cmd, capture_output=True, text=True, timeout=15)
        logger.debug("Restarted com.android.launcher3")
    except Exception:
        logger.debug("Launcher restart failed", exc_info=True)


# ---------------------------------------------------------------------------
# Non-rooted path
# ---------------------------------------------------------------------------

def _set_wallpaper_non_rooted(adb: list[str]) -> None:
    """Attempt to apply the wallpaper via the Android 13+ cmd wallpaper interface."""
    # 'cmd wallpaper set-wallpaper' is available from Android 13 (API 33)
    set_cmd = adb + [
        "shell", "cmd", "wallpaper", "set-wallpaper",
        "--file", _REMOTE_WALLPAPER_PATH,
        "--which", "both",
    ]
    try:
        proc = subprocess.run(set_cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode == 0 and "error" not in proc.stdout.lower():
            logger.info("Wallpaper applied via 'cmd wallpaper set-wallpaper'")
            return
        logger.debug(
            "'cmd wallpaper set-wallpaper' unavailable or failed: %s %s",
            proc.stdout.strip(), proc.stderr.strip(),
        )
    except Exception:
        logger.debug("'cmd wallpaper' raised", exc_info=True)

    # Final fallback: instruct user
    logger.info(
        "Automatic wallpaper setting was not supported on this device. "
        "The wallpaper image has been saved to %s. "
        "Open the Gallery or Files app on your Android device and set it as "
        "the wallpaper manually.",
        _REMOTE_WALLPAPER_PATH,
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _adb_shell(adb: list[str], shell_args: list[str]) -> None:
    try:
        subprocess.run(adb + ["shell"] + shell_args, capture_output=True, text=True, timeout=15)
    except Exception:
        logger.debug("adb shell %s failed", shell_args, exc_info=True)
