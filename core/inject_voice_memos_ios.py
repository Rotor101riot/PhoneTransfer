from __future__ import annotations

import logging
from pathlib import Path

from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)

# Non-jailbroken fallback: write to Media volume (user must manually import)
_AFC_FALLBACK_DIR = "/var/mobile/Media/PhoneTransfer/voice_memos"

# Jailbroken: app container paths
_APP_CONTAINERS_ROOT = "/var/mobile/Containers/Data/Application"
_CONTAINER_METADATA_PLIST = ".com.apple.mobile_container_manager.metadata.plist"
_VOICEMEMOS_BUNDLE = "com.apple.VoiceMemos"


def inject(device_id: str, items: list, staging_dir: Path, is_privileged: bool) -> int:
    """Inject voice memos onto an iOS device.

    Jailbroken: Locates the VoiceMemos app container via AFC2 and writes .m4a
    files directly into its Documents/ directory.

    Non-jailbroken: Standard AFC only exposes /var/mobile/Media/.  Files are
    pushed to /var/mobile/Media/PhoneTransfer/voice_memos/ and the user is
    instructed to import them manually.

    Args:
        device_id: UDID of the iOS device.
        items: List of MediaFile objects; only album="voice_memo" are processed.
        staging_dir: Staging root (unused for direct push but kept for signature).
        is_privileged: True if device is jailbroken.

    Returns:
        Count of files successfully pushed.
    """
    memos = [mf for mf in items if isinstance(mf, MediaFile) and mf.album == "voice_memo"]
    if not memos:
        logger.info("No voice memo items to inject onto iOS")
        return 0

    if is_privileged:
        return _inject_afc2(device_id, memos)
    return _inject_afc_fallback(device_id, memos)


# ---------------------------------------------------------------------------
# Jailbroken — AFC2 into VoiceMemos container
# ---------------------------------------------------------------------------

def _inject_afc2(udid: str, items: list[MediaFile]) -> int:
    try:
        from core.device_connection_cache import get_broker
        from core.afc2_connector import AFC2Connector

        broker = get_broker(udid)
        afc = AFC2Connector(broker)

        container_path = _find_voicememos_container(afc)
        if container_path is None:
            logger.warning(
                "VoiceMemos container not found via AFC2; "
                "falling back to /var/mobile/Media/PhoneTransfer/voice_memos"
            )
            return _push_items(afc, _AFC_FALLBACK_DIR, items, fallback=True)

        docs_dir = f"{container_path}/Documents"
        count = _push_items(afc, docs_dir, items, fallback=False)
        if count:
            logger.info(
                "Pushed %d voice memo(s) to VoiceMemos container. "
                "The VoiceMemos app may need to be relaunched to display new files.",
                count,
            )
        return count

    except Exception:
        logger.exception("AFC2 voice memo injection failed for device %s", udid)
        return 0


def _find_voicememos_container(afc) -> str | None:
    """Return the app container path for com.apple.VoiceMemos, or None."""
    uuids = afc.list_dir(_APP_CONTAINERS_ROOT)
    if not uuids:
        logger.debug("Cannot list %s via AFC2", _APP_CONTAINERS_ROOT)
        return None

    for uuid_entry in uuids:
        plist_path = f"{_APP_CONTAINERS_ROOT}/{uuid_entry}/{_CONTAINER_METADATA_PLIST}"
        data = afc.read_file(plist_path)
        if data and _VOICEMEMOS_BUNDLE.encode() in data:
            return f"{_APP_CONTAINERS_ROOT}/{uuid_entry}"

    return None


# ---------------------------------------------------------------------------
# Non-jailbroken — standard AFC fallback
# ---------------------------------------------------------------------------

def _inject_afc_fallback(udid: str, items: list[MediaFile]) -> int:
    try:
        from core.device_connection_cache import get_broker
        from core.afc_connector import AFCConnector

        broker = get_broker(udid)
        afc = AFCConnector(broker)
        count = _push_items(afc, _AFC_FALLBACK_DIR, items, fallback=True)
        return count

    except Exception:
        logger.exception("AFC voice memo injection failed for device %s", udid)
        return 0


# ---------------------------------------------------------------------------
# Shared push helper
# ---------------------------------------------------------------------------

def _push_items(afc, remote_dir: str, items: list[MediaFile], *, fallback: bool) -> int:
    """Push .m4a files via the given AFC handle into remote_dir."""
    afc.makedirs(remote_dir)

    count = 0
    for mf in items:
        src = mf.local_path
        if not src or not src.exists():
            logger.warning("Source file missing for voice memo %s", mf.filename)
            continue

        remote_path = f"{remote_dir}/{mf.filename}"
        if afc.push_file(src, remote_path):
            logger.debug("Pushed voice memo %s -> %s", src, remote_path)
            count += 1
        else:
            logger.warning("Failed to push %s to %s", src, remote_path)

    if count and fallback:
        logger.info(
            "Pushed %d voice memo(s) to %s. "
            "NOTE: On non-jailbroken devices this path is not accessible by the "
            "VoiceMemos app.  Use Files.app or iTunes File Sharing to import "
            "the files manually.",
            count, remote_dir,
        )

    return count
