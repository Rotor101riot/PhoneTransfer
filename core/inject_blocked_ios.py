"""
inject_blocked_ios.py

Inject blocked phone numbers into iOS.

Two paths:
  * **Backup-mod (default)**: when an :class:`IOSBackupInjector` session is
    active, stage ``HomeDomain:Library/Preferences/com.apple.cmfsyncagent.plist``
    and append entries to ``__kCMFBlockListStoreTopLevelKey →
    __kCMFBlockListStoreArrayKey``.  Modern iOS (16+) routes blocked
    numbers through CMF (CommunicationsFilter), not the legacy
    ``com.apple.preferences.blocked.plist`` or ``ZBLOCKEDENTRY`` table.
    The store revision counter is bumped on every change.
  * **AFC2 (legacy)**: pull/push the legacy
    ``com.apple.preferences.blocked.plist`` through AFC2 on a jailbroken
    device.  Kept as a fallback for the rare case where the caller
    intentionally bypassed the backup-mod orchestrator.

Non-jailbroken without a backup session: HomeDomain isn't writable via
standard AFC, so we log guidance and return 0.
"""

from __future__ import annotations

import logging
import plistlib
import re
from datetime import datetime, timezone
from pathlib import Path

from core.ios_backup_injector import IOSBackupInjector, get_current_injector
from core.normalization_schema import BlockedNumber

logger = logging.getLogger(__name__)

_CMF_DOMAIN = "HomeDomain"
_CMF_RELPATH = "Library/Preferences/com.apple.cmfsyncagent.plist"

_LEGACY_DEVICE_PATH = "/var/mobile/Library/Preferences/com.apple.preferences.blocked.plist"

# CMF dict keys (constants, do not localize).
_K_TOPLEVEL = "__kCMFBlockListStoreTopLevelKey"
_K_TYPE = "__kCMFBlockListStoreTypeKey"
_K_TYPE_VALUE = "__kCMFBlockListStoreTypeValue"
_K_ARRAY = "__kCMFBlockListStoreArrayKey"
_K_REVISION = "__kCMFBlockListStoreRevisionKey"
_K_REVISION_TS = "__kCMFBlockListStoreRevisionTimestampKey"
_K_VERSION = "__kCMFBlockListStoreVersionKey"

_K_ITEM_TYPE = "__kCMFItemTypeKey"
_K_ITEM_VERSION = "__kCMFItemVersionKey"
_K_ITEM_PHONE = "__kCMFItemPhoneNumberUnformattedKey"
_K_ITEM_PHONE_CC = "__kCMFItemPhoneNumberCountryCodeKey"

_ITEM_TYPE_PHONE = 0


def inject(
    device_id: str,
    items: list[BlockedNumber],
    staging_dir: Path,
    is_jailbroken: bool,
) -> int:
    if not items:
        logger.debug("inject_blocked_ios: no items to inject")
        return 0

    injector = get_current_injector()
    if injector is not None:
        try:
            count = _inject_via_backup(injector, items)
            logger.info(
                "inject_blocked_ios: staged %d blocked entry(ies) into the "
                "backup for %s", count, device_id,
            )
            return count
        except Exception as exc:
            logger.warning(
                "inject_blocked_ios: backup-mod path failed (%s) — "
                "falling back to AFC2", exc,
            )

    if not is_jailbroken:
        logger.warning(
            "inject_blocked_ios: injecting blocked numbers into a "
            "non-jailbroken iOS device is not supported without an active "
            "backup-mod session. The blocked-numbers preference file lives "
            "in HomeDomain and isn't writable via standard AFC. Either "
            "run inside a backup-mod pipeline or jailbreak the device."
        )
        return 0

    return _inject_afc2_legacy(device_id, items)


# ---------------------------------------------------------------------------
# Backup-mod path (modern iOS, CMF)
# ---------------------------------------------------------------------------

def _inject_via_backup(
    injector: IOSBackupInjector, items: list[BlockedNumber]
) -> int:
    plist_local = injector.stage_db(_CMF_DOMAIN, _CMF_RELPATH)
    pl = plistlib.loads(plist_local.read_bytes())

    top = pl.get(_K_TOPLEVEL)
    if not isinstance(top, dict):
        top = {
            _K_TYPE: _K_TYPE_VALUE,
            _K_ARRAY: [],
            _K_REVISION: 0,
            _K_REVISION_TS: datetime.now(timezone.utc).replace(tzinfo=None),
            _K_VERSION: 1,
        }
        pl[_K_TOPLEVEL] = top
    arr = top.setdefault(_K_ARRAY, [])

    existing_unformatted: set[str] = set()
    for entry in arr:
        if isinstance(entry, dict) and entry.get(_K_ITEM_TYPE) == _ITEM_TYPE_PHONE:
            num = entry.get(_K_ITEM_PHONE)
            if num:
                existing_unformatted.add(str(num))

    added = 0
    for item in items:
        unformatted = _strip_to_digits(item.number)
        if not unformatted or unformatted in existing_unformatted:
            continue
        arr.append({
            _K_ITEM_TYPE: _ITEM_TYPE_PHONE,
            _K_ITEM_VERSION: 1,
            _K_ITEM_PHONE: unformatted,
            _K_ITEM_PHONE_CC: "us",
        })
        existing_unformatted.add(unformatted)
        added += 1

    if added:
        top[_K_REVISION] = int(top.get(_K_REVISION, 0)) + 1
        top[_K_REVISION_TS] = datetime.now(timezone.utc).replace(tzinfo=None)
        plist_local.write_bytes(plistlib.dumps(pl, fmt=plistlib.FMT_BINARY))

    return added


def _strip_to_digits(number: str) -> str:
    """Reduce a phone number to digits only (CMF unformatted form)."""
    if not number:
        return ""
    return re.sub(r"\D", "", str(number))


# ---------------------------------------------------------------------------
# Legacy AFC2 path (older iOS, jailbroken)
# ---------------------------------------------------------------------------

def _inject_afc2_legacy(device_id: str, items: list[BlockedNumber]) -> int:
    try:
        from core.afc2_connector import AFC2Connector  # type: ignore[import]
    except ImportError:
        logger.error(
            "inject_blocked_ios: AFC2Connector is not available; "
            "ensure core/afc2_connector.py is present and its dependencies "
            "are installed"
        )
        return 0

    existing_numbers: set[str] = set()
    existing_entries: list[dict] = []

    try:
        with AFC2Connector(device_id) as afc2:
            raw = afc2.read_file(_LEGACY_DEVICE_PATH)
        if raw:
            try:
                pl = plistlib.loads(raw)
                existing_entries = pl.get("blockedList", [])
                if not isinstance(existing_entries, list):
                    existing_entries = []
                for entry in existing_entries:
                    if isinstance(entry, dict) and entry.get("phoneNumber"):
                        existing_numbers.add(str(entry["phoneNumber"]))
            except Exception as exc:
                logger.warning(
                    "inject_blocked_ios: could not parse legacy plist, will "
                    "overwrite: %s", exc,
                )
                existing_entries = []
    except Exception as exc:
        logger.warning(
            "inject_blocked_ios: could not read legacy plist (will create "
            "fresh): %s", exc,
        )
        existing_entries = []

    new_entries: list[dict] = []
    for item in items:
        if item.number in existing_numbers:
            continue
        new_entries.append({
            "phoneNumber": item.number,
            "userDefinedName": item.name or "",
        })
        existing_numbers.add(item.number)

    if not new_entries:
        return 0

    merged = existing_entries + new_entries
    plist_bytes = plistlib.dumps({"blockedList": merged}, fmt=plistlib.FMT_XML)

    try:
        with AFC2Connector(device_id) as afc2:
            afc2.write_file(_LEGACY_DEVICE_PATH, plist_bytes)
    except Exception as exc:
        logger.error(
            "inject_blocked_ios: failed to write legacy plist (device_id=%s): %s",
            device_id, exc,
        )
        return 0

    logger.info(
        "inject_blocked_ios: added %d blocked number(s) to legacy plist on %s",
        len(new_entries), device_id,
    )
    return len(new_entries)
