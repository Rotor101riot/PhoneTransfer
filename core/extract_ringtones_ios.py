from __future__ import annotations

import logging
from pathlib import Path

from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)

_STAGING_SUBDIR = "ringtones_ios"

# Paths relative to the AFC root (/var/mobile/Media)
_AFC_RINGTONE_PATHS = [
    "/iTunes_Control/Ringtones",
]

# Paths for AFC2 — root is '/', so full filesystem paths are used
_AFC2_RINGTONE_PATHS = [
    "/var/mobile/Library/Ringtones",
    "/var/mobile/Media/iTunes_Control/Ringtones",
]


def extract(device_id: str, staging_dir: Path, is_privileged: bool) -> list:
    """Extract custom ringtones from an iOS device.

    Uses AFC2 (jailbroken) or standard AFC (non-jailbroken) to pull .m4r files.

    Args:
        device_id: The UDID of the iOS device.
        staging_dir: Root staging directory; files are saved under ringtones_ios/.
        is_privileged: True if the device is jailbroken (AFC2 available).

    Returns:
        list[MediaFile] of extracted ringtones, or [] on failure.
    """
    out_dir = staging_dir / _STAGING_SUBDIR
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("Failed to create staging directory %s", out_dir)
        return []

    if is_privileged:
        return _extract_afc2(device_id, out_dir)
    return _extract_afc(device_id, out_dir)


# ---------------------------------------------------------------------------
# Jailbroken path — AFC2
# ---------------------------------------------------------------------------

def _extract_afc2(udid: str, out_dir: Path) -> list:
    try:
        from core.device_connection_cache import get_broker
        from core.afc2_connector import AFC2Connector

        broker = get_broker(udid)
        afc2 = AFC2Connector(broker)
        return _pull_m4r_from_afc(afc2, _AFC2_RINGTONE_PATHS, out_dir)

    except Exception:
        logger.exception("AFC2 ringtone extraction failed for device %s", udid)
        return []


# ---------------------------------------------------------------------------
# Non-jailbroken path — standard AFC
# ---------------------------------------------------------------------------

def _extract_afc(udid: str, out_dir: Path) -> list:
    try:
        from core.device_connection_cache import get_broker
        from core.afc_connector import AFCConnector

        broker = get_broker(udid)
        afc = AFCConnector(broker)
        return _pull_m4r_from_afc(afc, _AFC_RINGTONE_PATHS, out_dir)

    except Exception:
        logger.exception("AFC ringtone extraction failed for device %s", udid)
        return []


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _pull_m4r_from_afc(afc, remote_paths: list[str], out_dir: Path) -> list:
    """List remote directories and pull every .m4r file found."""
    results: list[MediaFile] = []

    for remote_dir in remote_paths:
        entries = afc.list_dir(remote_dir)
        if not entries:
            logger.debug("Cannot list AFC path %s — skipping", remote_dir)
            continue

        for entry in entries:
            if not entry.lower().endswith(".m4r"):
                continue

            remote_path = f"{remote_dir}/{entry}"
            local_path = out_dir / entry

            data = afc.read_file(remote_path)
            if data is None:
                logger.warning("Failed to pull ringtone %s", remote_path)
                continue
            try:
                local_path.write_bytes(data)
                logger.debug("Pulled ringtone %s -> %s", remote_path, local_path)
            except Exception:
                logger.warning("Failed to write ringtone %s", local_path, exc_info=True)
                continue

            results.append(
                MediaFile(
                    filename=entry,
                    mime_type="audio/x-m4r",
                    local_path=local_path,
                    created=None,
                    album="ringtone",
                    latitude=None,
                    longitude=None,
                )
            )

    logger.info("Extracted %d iOS ringtone(s)", len(results))
    return results
