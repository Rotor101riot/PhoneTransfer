"""
inject_wallpaper_ios.py

Inject wallpaper images into an iOS device.

Two paths:
  * **Backup-mod (default)**: when an :class:`IOSBackupInjector` session is
    active, override the SpringBoard thumbnails inside the encrypted
    backup so that the next restore paints the new lock/home wallpaper.
    Mirrors the strategy proved out by ``G:/test/modify_wallpaper.py``.
  * **AFC2 (jailbroken only)**: write the JPG directly to
    ``/var/mobile/Library/SpringBoard/{Lock,Home}Background.jpg``.  Kept
    as a fallback for the rare jailbroken-device case where the caller
    intentionally bypassed the backup-mod orchestrator.

The thumbnails are what iMazing / iOS's restore previewer surface; the
real ``LockBackground.cpbitmap`` next to them stays untouched (Apple's
proprietary bitmap format) but the device regenerates it from the JPG
seed on first SpringBoard launch after restore.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.ios_backup_injector import IOSBackupInjector, get_current_injector
from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)


_SPRINGBOARD_DOMAIN = "HomeDomain"
_SPRINGBOARD_DIR = "Library/SpringBoard"

# Thumbnail JPGs iOS regenerates the cpbitmap from on first SpringBoard
# launch after a restore.  Overriding these is enough to change the
# visible wallpaper.
_LOCK_THUMB = f"{_SPRINGBOARD_DIR}/LockBackgroundThumbnail.jpg"
_HOME_THUMB = f"{_SPRINGBOARD_DIR}/HomeBackgroundThumbnail.jpg"

# Jailbroken AFC2 paths (legacy fallback path only)
_SPRINGBOARD_LOCK = "/var/mobile/Library/SpringBoard/LockBackground.jpg"
_SPRINGBOARD_HOME = "/var/mobile/Library/SpringBoard/HomeBackground.jpg"


def inject(device_id: str, items: list, staging_dir: Path, is_privileged: bool) -> int:
    wallpapers = [
        mf for mf in items
        if isinstance(mf, MediaFile) and str(mf.album or "").startswith("wallpaper")
    ]
    if not wallpapers:
        logger.info("No wallpaper items to inject onto iOS")
        return 0

    injector = get_current_injector()
    if injector is not None:
        return _inject_via_backup(injector, wallpapers)

    if not is_privileged:
        logger.warning(
            "iOS wallpaper injection requires either an active backup "
            "injector session or a jailbroken device.  Skipping."
        )
        return 0

    return _inject_afc2(device_id, wallpapers)


# ---------------------------------------------------------------------------
# Backup-mod path
# ---------------------------------------------------------------------------

def _inject_via_backup(
    injector: IOSBackupInjector, items: list[MediaFile]
) -> int:
    written = 0
    for mf in items:
        src = mf.local_path
        if not src or not Path(src).exists():
            logger.warning("Source file missing for wallpaper %s", mf.filename)
            continue

        data = Path(src).read_bytes()
        if not data:
            continue

        album = str(mf.album or "")
        targets: list[str]
        if album == "wallpaper_lock":
            targets = [_LOCK_THUMB]
        elif album == "wallpaper_home":
            targets = [_HOME_THUMB]
        else:
            targets = [_LOCK_THUMB, _HOME_THUMB]

        for rel in targets:
            injector.stage_override(_SPRINGBOARD_DOMAIN, rel, data)
            logger.debug("Staged wallpaper override %s/%s (%d bytes)",
                         _SPRINGBOARD_DOMAIN, rel, len(data))
            written += 1

    if written:
        logger.info(
            "Staged %d wallpaper override(s) into the active backup; "
            "they will apply at the next restore.",
            written,
        )
    return written


# ---------------------------------------------------------------------------
# Jailbroken — AFC2
# ---------------------------------------------------------------------------

def _inject_afc2(udid: str, items: list[MediaFile]) -> int:
    try:
        from core.device_connection_cache import get_broker
        from core.afc2_connector import AFC2Connector

        broker = get_broker(udid)
        afc = AFC2Connector(broker)

        afc.makedirs("/var/mobile/Library/SpringBoard")

        pushed_any = False
        for mf in items:
            src = mf.local_path
            if not src or not Path(src).exists():
                logger.warning("Source file missing for wallpaper %s", mf.filename)
                continue

            data = Path(src).read_bytes()
            album = str(mf.album or "")

            if album == "wallpaper_lock":
                targets = [_SPRINGBOARD_LOCK]
            elif album == "wallpaper_home":
                targets = [_SPRINGBOARD_HOME]
            else:
                targets = [_SPRINGBOARD_LOCK, _SPRINGBOARD_HOME]

            for remote_path in targets:
                if afc.write_file(remote_path, data):
                    logger.debug("Wrote wallpaper to %s", remote_path)
                    pushed_any = True
                else:
                    logger.warning("Failed to write wallpaper to %s", remote_path)

        if pushed_any:
            logger.info(
                "Wallpaper written to SpringBoard paths.  A SpringBoard "
                "restart (ldrestart) or reboot is required for the change "
                "to take effect."
            )
            return 1

        return 0

    except Exception:
        logger.exception("AFC2 wallpaper injection failed for device %s", udid)
        return 0
