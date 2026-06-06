"""
inject_contact_groups_android.py

Injects contact groups into an Android device via the companion APK's
``inject_contact_groups`` command.

The companion APK uses ContentResolver.insert() on ContactsContract.Groups
to create each group.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.companion_app_protocol import CompanionClient, setup_adb_forward, teardown_adb_forward
from core.config_loader import get_config
from core.normalization_schema import ContactGroup

logger = logging.getLogger(__name__)


def inject(
    serial: str,
    items: list[ContactGroup],
    staging_dir: Path,
    is_rooted: bool = False,
) -> int:
    """
    Inject contact groups into the Android device via the companion APK.

    Parameters
    ----------
    serial:      ADB device serial.
    items:       ContactGroup objects to inject.
    staging_dir: Local directory for temporary files (unused).
    is_rooted:   Unused.

    Returns
    -------
    Number of groups successfully injected.
    """
    if not items:
        logger.info("[contact_groups/android] No contact groups to inject.")
        return 0

    try:
        from core.adb_manager import ADBManager
        adb = ADBManager(get_config())
        setup_adb_forward(adb, serial)
    except Exception as exc:
        logger.error("[contact_groups/android] ADB forward setup failed: %s", exc)
        return 0

    try:
        with CompanionClient() as client:
            if not client.ping():
                logger.error(
                    "[contact_groups/android] Companion APK not responding on %s",
                    serial,
                )
                return 0

            # Serialise ContactGroup items for the companion APK
            data = []
            for group in items:
                data.append({
                    "title": group.title,
                    "account_name": group.account_name or "phone",
                    "account_type": group.account_type or "phone",
                    "visible": group.visible,
                    "notes": group.notes,
                })

            response = client.send_recv({
                "cmd": "inject_contact_groups",
                "data": data,
            })

        if response.get("status") != "ok":
            logger.warning(
                "[contact_groups/android] APK inject returned status '%s': %s",
                response.get("status"), response.get("message"),
            )
            return 0

        injected = int(response.get("injected", 0))
        failed = int(response.get("failed", 0))
        logger.info(
            "[contact_groups/android] Injected %d group(s), %d failed on %s",
            injected, failed, serial,
        )
        return injected

    except Exception:
        logger.exception("[contact_groups/android] Unhandled error during injection")
        return 0
    finally:
        try:
            teardown_adb_forward(adb, serial)
        except Exception:
            pass
