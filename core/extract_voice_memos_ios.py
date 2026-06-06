from __future__ import annotations

import logging
from pathlib import Path

from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)

_STAGING_SUBDIR = "voicememos_ios"

# App bundle ID for Voice Memos
_VOICEMEMOS_BUNDLE = "com.apple.VoiceMemos"

# iOSbackup domain for Voice Memos app sandbox
_BACKUP_DOMAIN = f"AppDomain-{_VOICEMEMOS_BUNDLE}"

# AFC2 container metadata plist key / value
_CONTAINER_METADATA_PLIST = ".com.apple.mobile_container_manager.metadata.plist"
_METADATA_KEY = "MCMMetadataIdentifier"

# Root path when searching AFC2 app containers
_APP_CONTAINERS_ROOT = "/var/mobile/Containers/Data/Application"


def extract(device_id: str, staging_dir: Path, is_privileged: bool) -> list:
    """Extract voice memos from an iOS device.

    For jailbroken devices, enumerates app containers via AFC2 to locate the
    VoiceMemos sandbox and pulls .m4a files directly.  For non-jailbroken
    devices, falls back to iOSbackup using the AppDomain for VoiceMemos.

    Args:
        device_id: UDID of the iOS device.
        staging_dir: Root staging directory; files saved under voicememos_ios/.
        is_privileged: True if the device is jailbroken.

    Returns:
        list[MediaFile] with album="voice_memo", or [] on failure.
    """
    out_dir = staging_dir / _STAGING_SUBDIR
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("Failed to create staging directory %s", out_dir)
        return []

    if is_privileged:
        result = _extract_afc2(device_id, out_dir)
        if result:
            return result
        logger.info("AFC2 voice memo extraction returned nothing; falling back to iOSbackup")

    return _extract_iosbackup(device_id, out_dir)


# ---------------------------------------------------------------------------
# Jailbroken — AFC2 container enumeration
# ---------------------------------------------------------------------------

def _extract_afc2(udid: str, out_dir: Path) -> list:
    try:
        from core.device_connection_cache import get_broker
        from core.afc2_connector import AFC2Connector

        broker = get_broker(udid)
        afc = AFC2Connector(broker)

        # Find the VoiceMemos container UUID
        container_path = _find_voicememos_container(afc)
        if container_path is None:
            logger.debug("Could not locate VoiceMemos container via AFC2")
            return []

        docs_path = f"{container_path}/Documents"
        return _pull_m4a_files(afc, docs_path, out_dir)

    except Exception:
        logger.exception("AFC2 voice memo extraction failed for device %s", udid)
        return []


def _find_voicememos_container(afc) -> str | None:
    """Enumerate app containers to find the VoiceMemos sandbox path."""
    uuids = afc.list_dir(_APP_CONTAINERS_ROOT)
    if not uuids:
        logger.debug("Cannot list %s", _APP_CONTAINERS_ROOT)
        return None

    for uuid_entry in uuids:
        plist_path = f"{_APP_CONTAINERS_ROOT}/{uuid_entry}/{_CONTAINER_METADATA_PLIST}"
        data = afc.read_file(plist_path)
        if data and _VOICEMEMOS_BUNDLE.encode() in data:
            return f"{_APP_CONTAINERS_ROOT}/{uuid_entry}"

    return None


def _pull_m4a_files(afc, remote_dir: str, out_dir: Path) -> list[MediaFile]:
    """Recursively pull .m4a files from a remote AFC directory."""
    results: list[MediaFile] = []
    entries = afc.list_dir(remote_dir)
    if not entries:
        logger.debug("Cannot list AFC path %s", remote_dir)
        return results

    for entry in entries:
        if entry in (".", ".."):
            continue
        remote_path = f"{remote_dir}/{entry}"
        if entry.lower().endswith(".m4a"):
            local_path = out_dir / entry
            data = afc.read_file(remote_path)
            if data is None:
                logger.warning("Failed to pull %s", remote_path)
                continue
            try:
                local_path.write_bytes(data)
                logger.debug("Pulled voice memo %s", remote_path)
                results.append(
                    MediaFile(
                        filename=entry,
                        mime_type="audio/m4a",
                        local_path=local_path,
                        created=None,
                        album="voice_memo",
                        latitude=None,
                        longitude=None,
                    )
                )
            except Exception:
                logger.warning("Failed to write %s", local_path, exc_info=True)
        else:
            # Recurse into subdirectories
            info = afc.stat(remote_path)
            if info and info.get("st_ifmt") == "S_IFDIR":
                results.extend(_pull_m4a_files(afc, remote_path, out_dir))

    return results


# ---------------------------------------------------------------------------
# Non-jailbroken — iOSbackup
# ---------------------------------------------------------------------------

def _extract_iosbackup(udid: str, out_dir: Path) -> list:
    results: list[MediaFile] = []
    try:
        from core.device_connection_cache import get_iosbackup
        backup = get_iosbackup(udid)
        file_list = backup.getBackupFilesList()
    except Exception:
        logger.exception("Failed to open iOSbackup for device %s", udid)
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    for entry in file_list:
        if entry.get("domain") != _BACKUP_DOMAIN:
            continue
        relative = entry.get("relativePath", "") or entry.get("name", "")
        if not relative.lower().endswith(".m4a"):
            continue

        filename = Path(relative).name
        local_path = out_dir / filename
        try:
            info = backup.getFileDecryptedCopy(
                relativePath=relative,
                targetName=filename,
                targetFolder=str(out_dir),
            )
            if info and local_path.exists():
                logger.debug("Restored voice memo %s from backup", filename)
                results.append(
                    MediaFile(
                        filename=filename,
                        mime_type="audio/m4a",
                        local_path=local_path,
                        created=None,
                        album="voice_memo",
                        latitude=None,
                        longitude=None,
                    )
                )
        except Exception:
            logger.warning("Failed to decrypt backup file %s", relative, exc_info=True)

    logger.info("Extracted %d iOS voice memo(s) via iOSbackup", len(results))
    return results
