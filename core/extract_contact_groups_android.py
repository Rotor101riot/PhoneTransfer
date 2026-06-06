"""
extract_contact_groups_android.py

Extracts contact groups from an Android device via the companion APK's
``extract_contact_groups`` command.

The companion APK queries ContactsContract.Groups and returns a JSON
array of group objects with: group_id, title, account_name, account_type,
visible, notes, member_count.

Returns a list of ContactGroup objects (normalization_schema.py).
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.companion_app_protocol import CompanionClient, setup_adb_forward, teardown_adb_forward
from core.config_loader import get_config
from core.normalization_schema import ContactGroup

logger = logging.getLogger(__name__)


def extract(
    serial: str,
    staging_dir: Path,
    is_rooted: bool = False,
) -> list[ContactGroup]:
    """
    Extract contact groups from the Android device via the companion APK.

    Parameters
    ----------
    serial:      ADB device serial string.
    staging_dir: Root staging directory for this transfer session.
    is_rooted:   Unused (companion APK does not require root for groups).

    Returns
    -------
    List of ContactGroup objects; empty list on failure.
    """
    try:
        from core.adb_manager import ADBManager
        adb = ADBManager(get_config())
        setup_adb_forward(adb, serial)
    except Exception as exc:
        logger.error("[contact_groups/android] ADB forward setup failed: %s", exc)
        return []

    try:
        with CompanionClient() as client:
            if not client.ping():
                logger.error(
                    "[contact_groups/android] Companion APK not responding on %s",
                    serial,
                )
                return []

            response = client.send_recv({"cmd": "extract_contact_groups"})

        if response.get("status") != "ok":
            logger.warning(
                "[contact_groups/android] APK returned status '%s': %s",
                response.get("status"), response.get("message"),
            )
            return []

        raw_items: list[dict] = response.get("data", [])
        if not raw_items:
            logger.info("[contact_groups/android] No contact groups found")
            return []

        groups: list[ContactGroup] = []
        for raw in raw_items:
            try:
                groups.append(ContactGroup(
                    title=raw.get("title", ""),
                    group_id=int(raw["group_id"]) if raw.get("group_id") is not None else None,
                    account_name=raw.get("account_name"),
                    account_type=raw.get("account_type"),
                    visible=bool(raw.get("visible", True)),
                    notes=raw.get("notes"),
                    member_count=int(raw.get("member_count", 0)),
                ))
            except Exception as exc:
                logger.debug(
                    "[contact_groups/android] Skipping malformed group: %s", exc
                )

        logger.info(
            "[contact_groups/android] Extracted %d contact group(s) from %s",
            len(groups), serial,
        )
        return groups

    except Exception:
        logger.exception("[contact_groups/android] Unhandled error during extraction")
        return []
    finally:
        try:
            teardown_adb_forward(adb, serial)
        except Exception:
            pass
