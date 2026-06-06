"""
inject_clipboard_android.py

Injects clipboard contents into an Android device via the companion APK's
``inject_clipboard`` command.

The companion APK sets the primary clip via ClipboardManager.setPrimaryClip().
Only the first ClipboardItem is injected (Android has a single primary clip).
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.companion_app_protocol import CompanionClient, setup_adb_forward, teardown_adb_forward
from core.config_loader import get_config
from core.normalization_schema import ClipboardItem

logger = logging.getLogger(__name__)


def inject(
    serial: str,
    items: list[ClipboardItem],
    staging_dir: Path,
    is_rooted: bool = False,
) -> int:
    """
    Inject clipboard contents into the Android device via the companion APK.

    Parameters
    ----------
    serial:      ADB device serial.
    items:       ClipboardItem objects to inject (only the first is used).
    staging_dir: Local directory for temporary files (unused).
    is_rooted:   Unused.

    Returns
    -------
    Number of items successfully injected (0 or 1).
    """
    if not items:
        logger.info("[clipboard/android] No clipboard items to inject.")
        return 0

    try:
        from core.adb_manager import ADBManager
        adb = ADBManager(get_config())
        setup_adb_forward(adb, serial)
    except Exception as exc:
        logger.error("[clipboard/android] ADB forward setup failed: %s", exc)
        return 0

    try:
        with CompanionClient() as client:
            if not client.ping():
                logger.error(
                    "[clipboard/android] Companion APK not responding on %s",
                    serial,
                )
                return 0

            data = [{"text": item.text, "mime_type": item.mime_type} for item in items]

            response = client.send_recv({
                "cmd": "inject_clipboard",
                "data": data,
            })

        if response.get("status") != "ok":
            logger.warning(
                "[clipboard/android] APK inject returned status '%s': %s",
                response.get("status"), response.get("message"),
            )
            return 0

        injected = int(response.get("injected", 0))
        logger.info(
            "[clipboard/android] Injected %d clipboard item(s) into %s",
            injected, serial,
        )
        return injected

    except Exception:
        logger.exception("[clipboard/android] Unhandled error during injection")
        return 0
    finally:
        try:
            teardown_adb_forward(adb, serial)
        except Exception:
            pass
