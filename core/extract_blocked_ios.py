from __future__ import annotations

import logging
import plistlib
from pathlib import Path

from core.models import BlockedNumber

logger = logging.getLogger(__name__)


def extract(device_id: str, staging_dir: Path, is_jailbroken: bool) -> list[BlockedNumber]:
    """Extract blocked phone numbers from an iOS device.

    Non-jailbroken: reads from MobileSync backup via iOSbackup.
    Jailbroken: reads directly via AFC2 from the live filesystem.
    """
    if is_jailbroken:
        return _extract_jailbroken(device_id, staging_dir)
    return _extract_backup(device_id, staging_dir)


def _parse_plist_data(data: bytes) -> list[BlockedNumber]:
    """Parse binary or XML plist bytes and return a list of BlockedNumber."""
    try:
        pl = plistlib.loads(data)
    except Exception as exc:
        logger.error("extract_blocked_ios: failed to parse plist: %s", exc)
        return []

    results: list[BlockedNumber] = []

    # Format 1: {"blockedList": [{"phoneNumber": ..., "userDefinedName": ...}, ...]}
    blocked_list = pl.get("blockedList")
    if isinstance(blocked_list, list):
        for entry in blocked_list:
            if not isinstance(entry, dict):
                continue
            number = entry.get("phoneNumber") or entry.get("phoneNumber ")
            if not number:
                continue
            name = entry.get("userDefinedName") or entry.get("name") or None
            if name == "":
                name = None
            results.append(BlockedNumber(number=str(number), name=name))
        if results:
            logger.debug("extract_blocked_ios: parsed %d entries via 'blockedList' key", len(results))
            return results

    # Format 2: {"BlockedPhoneNumbers": ["+15551234567", ...]}
    blocked_numbers = pl.get("BlockedPhoneNumbers")
    if isinstance(blocked_numbers, list):
        for entry in blocked_numbers:
            if isinstance(entry, str) and entry:
                results.append(BlockedNumber(number=entry))
            elif isinstance(entry, dict):
                number = entry.get("phoneNumber")
                if number:
                    name = entry.get("userDefinedName") or entry.get("name") or None
                    if name == "":
                        name = None
                    results.append(BlockedNumber(number=str(number), name=name))
        if results:
            logger.debug(
                "extract_blocked_ios: parsed %d entries via 'BlockedPhoneNumbers' key", len(results)
            )
            return results

    logger.warning(
        "extract_blocked_ios: plist had no recognised blocked-number keys; top-level keys: %s",
        list(pl.keys()),
    )
    return results


def _extract_backup(device_id: str, staging_dir: Path) -> list[BlockedNumber]:
    """Extract via iOSbackup (non-jailbroken path)."""
    try:
        from core.device_connection_cache import get_iosbackup
    except ImportError:
        logger.error(
            "extract_blocked_ios: iOSbackup library is not installed; "
            "install it with: pip install iOSbackup"
        )
        return []

    plist_candidates = [
        ("HomeDomain", "Library/Preferences/com.apple.preferences.blocked.plist"),
        ("HomeDomain", "Library/Preferences/com.apple.cmfsyncagent.plist"),
    ]

    for domain, relative_path in plist_candidates:
        try:
            b = get_iosbackup(device_id)
            _result = b.getRelativePathDecryptedData(relativePath=relative_path)
            # Returns (info, bytes) for encrypted backups, raw bytes for unencrypted.
            data = _result[1] if isinstance(_result, tuple) else _result
            if not data:
                logger.debug(
                    "extract_blocked_ios: backup path %s/%s returned no data", domain, relative_path
                )
                continue

            results = _parse_plist_data(data)
            if results:
                logger.info(
                    "extract_blocked_ios: extracted %d blocked numbers from backup (%s/%s)",
                    len(results),
                    domain,
                    relative_path,
                )
                return results

        except Exception as exc:
            logger.debug(
                "extract_blocked_ios: could not read backup path %s/%s: %s",
                domain,
                relative_path,
                exc,
            )
            continue

    logger.warning(
        "extract_blocked_ios: no blocked numbers found in any backup plist candidate "
        "(device_id=%s). The backup may be encrypted or the device may have no blocked numbers.",
        device_id,
    )
    return []


def _extract_jailbroken(device_id: str, staging_dir: Path) -> list[BlockedNumber]:
    """Extract via AFC2 direct filesystem access (jailbroken path)."""
    try:
        from core.afc2_connector import AFC2Connector  # type: ignore[import]
    except ImportError:
        logger.error(
            "extract_blocked_ios: AFC2Connector is not available; "
            "ensure core/afc2_connector.py is present and its dependencies are installed"
        )
        return []

    plist_path = "/var/mobile/Library/Preferences/com.apple.preferences.blocked.plist"

    try:
        with AFC2Connector(device_id) as afc2:
            data = afc2.read_file(plist_path)
    except Exception as exc:
        logger.error(
            "extract_blocked_ios: AFC2 read failed for %s (device_id=%s): %s",
            plist_path,
            device_id,
            exc,
        )
        return []

    if not data:
        logger.warning(
            "extract_blocked_ios: AFC2 returned empty data for %s", plist_path
        )
        return []

    results = _parse_plist_data(data)
    logger.info(
        "extract_blocked_ios: extracted %d blocked numbers via AFC2 (jailbroken)",
        len(results),
    )
    return results
