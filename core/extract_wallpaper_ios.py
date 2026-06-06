from __future__ import annotations

import logging
from pathlib import Path

from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)

_STAGING_SUBDIR = "wallpaper_ios"

# AFC-relative path (AFC root = /var/mobile/Media)
_AFC_WALLPAPER_DIR  = "/PhotoData/Wallpapers"
# AFC2 full-filesystem path (AFC2 root = /)
_AFC2_WALLPAPER_DIR = "/var/mobile/Media/PhotoData/Wallpapers"

# Candidate filenames to probe (device versions vary)
_WALLPAPER_CANDIDATES = [
    "LockBackground.jpg",
    "LockBackground.cpbitmap",
    "LockBackground.png",
    "HomeBackground.jpg",
    "HomeBackground.cpbitmap",
    "HomeBackground.png",
]

# SpringBoard wallpaper files in iOS backups (HomeDomain)
# These are the cached/original wallpaper images stored by SpringBoard,
# separate from the /PhotoData/Wallpapers live device directory.
_SPRINGBOARD_WALLPAPER_FILES = [
    # cpbitmap format (iOS 7+) — device-specific resolution
    ("Library/SpringBoard/LockBackground.cpbitmap", "wallpaper_lock"),
    ("Library/SpringBoard/HomeBackground.cpbitmap", "wallpaper_home"),
    # Original JPEG images (pre-crop)
    ("Library/SpringBoard/OriginalLockBackground.jpg", "wallpaper_lock"),
    ("Library/SpringBoard/OriginalHomeBackground.jpg", "wallpaper_home"),
    # PNG variants (some iOS versions)
    ("Library/SpringBoard/LockBackground.png", "wallpaper_lock"),
    ("Library/SpringBoard/HomeBackground.png", "wallpaper_home"),
    # Thumbnail variants
    ("Library/SpringBoard/LockBackgroundThumbnail.jpg", "wallpaper_lock"),
    ("Library/SpringBoard/HomeBackgroundThumbnail.jpg", "wallpaper_home"),
]


def extract(device_id: str, staging_dir: Path, is_privileged: bool) -> list:
    """Extract the current wallpaper(s) from an iOS device.

    Extraction paths (tried in order):
    1. AFC2 (jailbroken) — full filesystem access to /var/mobile/Media/PhotoData/Wallpapers
    2. AFC (standard) — /var/mobile/Media is accessible without jailbreak
    3. Backup (SpringBoard) — extract from HomeDomain/Library/SpringBoard/ in backup

    Args:
        device_id: UDID of the iOS device.
        staging_dir: Root staging directory; files saved under wallpaper_ios/.
        is_privileged: True if device is jailbroken (AFC2 available).

    Returns:
        list[MediaFile] (typically 1-2 items), or [] on failure.
    """
    out_dir = staging_dir / _STAGING_SUBDIR
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("Failed to create staging directory %s", out_dir)
        return []

    # Path 1: AFC2 (jailbroken)
    if is_privileged:
        result = _extract_via_afc2(device_id, out_dir)
        if result:
            return result
        logger.debug("AFC2 wallpaper extraction empty; retrying with standard AFC")

    # Path 2: Standard AFC
    result = _extract_via_afc(device_id, out_dir)
    if result:
        return result

    # Path 3: SpringBoard backup domain fallback
    logger.debug("AFC wallpaper extraction empty; trying SpringBoard backup domain")
    return _extract_from_backup(device_id, out_dir)


# ---------------------------------------------------------------------------
# Jailbroken — AFC2 (broader directory access)
# ---------------------------------------------------------------------------

def _extract_via_afc2(udid: str, out_dir: Path) -> list:
    try:
        from core.device_connection_cache import get_broker
        from core.afc2_connector import AFC2Connector

        broker = get_broker(udid)
        afc2 = AFC2Connector(broker)
        return _pull_wallpapers(afc2, _AFC2_WALLPAPER_DIR, out_dir)
    except Exception:
        logger.exception("AFC2 wallpaper extraction failed for device %s", udid)
        return []


# ---------------------------------------------------------------------------
# Non-jailbroken — standard AFC (/var/mobile/Media is accessible)
# ---------------------------------------------------------------------------

def _extract_via_afc(udid: str, out_dir: Path) -> list:
    try:
        from core.device_connection_cache import get_broker
        from core.afc_connector import AFCConnector

        broker = get_broker(udid)
        afc = AFCConnector(broker)
        return _pull_wallpapers(afc, _AFC_WALLPAPER_DIR, out_dir)
    except Exception:
        logger.exception("AFC wallpaper extraction failed for device %s", udid)
        return []


# ---------------------------------------------------------------------------
# Shared pull logic
# ---------------------------------------------------------------------------

def _pull_wallpapers(afc, wallpaper_dir: str, out_dir: Path) -> list[MediaFile]:
    """List the wallpaper directory and pull recognised files."""
    results: list[MediaFile] = []

    # Try to enumerate the directory first so we catch any non-standard names.
    entries = afc.list_dir(wallpaper_dir)
    if not entries:
        logger.debug("Cannot list %s via AFC; probing known filenames", wallpaper_dir)
        entries = _WALLPAPER_CANDIDATES  # fall back to probing

    for entry in entries:
        remote_path = f"{wallpaper_dir}/{entry}"
        local_path = out_dir / entry

        data = afc.read_file(remote_path)
        if data is None:
            logger.debug("Cannot read wallpaper file %s", remote_path)
            continue

        if not data:
            continue

        try:
            local_path.write_bytes(data)
        except Exception:
            logger.warning("Failed to write wallpaper to %s", local_path, exc_info=True)
            continue

        album = _album_for_filename(entry)
        mime_type = _mime_for_filename(entry)
        logger.debug("Pulled wallpaper %s (album=%s)", entry, album)

        results.append(
            MediaFile(
                filename=entry,
                mime_type=mime_type,
                local_path=local_path,
                created=None,
                album=album,
                latitude=None,
                longitude=None,
            )
        )

    logger.info("Extracted %d iOS wallpaper file(s)", len(results))
    return results


def _album_for_filename(name: str) -> str:
    lower = name.lower()
    if "home" in lower:
        return "wallpaper_home"
    if "lock" in lower:
        return "wallpaper_lock"
    return "wallpaper_home"


def _mime_for_filename(name: str) -> str:
    ext = Path(name).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".cpbitmap": "image/jpeg",  # cpbitmap is Apple's compressed JPEG variant
    }.get(ext, "image/jpeg")


# ---------------------------------------------------------------------------
# Backup-based extraction — SpringBoard domain (Item #10)
# ---------------------------------------------------------------------------

def _extract_from_backup(udid: str, out_dir: Path) -> list[MediaFile]:
    """
    Extract wallpapers from an iOS backup's HomeDomain/Library/SpringBoard/.

    This is the fallback path when AFC/AFC2 cannot reach the wallpaper files
    (e.g., device not connected, extracting from a pre-existing backup).

    SpringBoard stores wallpapers as:
    - LockBackground.cpbitmap / HomeBackground.cpbitmap (device-resolution)
    - OriginalLockBackground.jpg / OriginalHomeBackground.jpg (pre-crop)
    """
    try:
        from core.device_connection_cache import get_backup_dir
    except ImportError:
        logger.debug("device_connection_cache not available for backup extraction")
        return []

    backup_dir = get_backup_dir(udid)
    if backup_dir is None:
        logger.debug("No backup directory found for device %s", udid)
        return []

    try:
        from core.backup_parser_ios import open_backup
    except ImportError:
        logger.debug("backup_parser_ios not available")
        return []

    results: list[MediaFile] = []
    seen_albums: set[str] = set()

    try:
        with open_backup(backup_dir) as bp:
            for relative_path, album in _SPRINGBOARD_WALLPAPER_FILES:
                # Prefer original JPEG over cpbitmap; skip if we already have
                # a wallpaper for this album (home/lock)
                if album in seen_albums:
                    continue

                data = bp.open_file("HomeDomain", relative_path)
                if data is None or len(data) == 0:
                    continue

                filename = Path(relative_path).name
                local_path = out_dir / filename

                try:
                    local_path.write_bytes(data)
                except OSError as exc:
                    logger.warning("Failed to write %s: %s", local_path, exc)
                    continue

                mime_type = _mime_for_filename(filename)
                logger.debug(
                    "Extracted SpringBoard wallpaper: %s (album=%s, %d bytes)",
                    filename, album, len(data),
                )

                results.append(MediaFile(
                    filename=filename,
                    mime_type=mime_type,
                    local_path=local_path,
                    created=None,
                    album=album,
                    latitude=None,
                    longitude=None,
                ))
                seen_albums.add(album)

    except Exception:
        logger.exception("SpringBoard backup wallpaper extraction failed for %s", udid)

    if results:
        logger.info("Extracted %d wallpaper(s) from SpringBoard backup domain", len(results))
    return results
