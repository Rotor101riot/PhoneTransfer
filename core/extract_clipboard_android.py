"""
extract_clipboard_android.py

Extracts clipboard contents from an Android device via the companion APK's
``extract_clipboard`` command.

The companion APK reads the primary clip via ClipboardManager and returns
a JSON array of items with: text, mime_type.

Returns a list of ClipboardItem objects (normalization_schema.py).
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.companion_app_protocol import CompanionClient, setup_adb_forward, teardown_adb_forward
from core.config_loader import get_config
from core.normalization_schema import ClipboardItem

logger = logging.getLogger(__name__)


def extract(
    serial: str,
    staging_dir: Path,
    is_rooted: bool = False,
) -> list[ClipboardItem]:
    """
    Extract clipboard contents from the Android device via the companion APK.

    Parameters
    ----------
    serial:      ADB device serial string.
    staging_dir: Root staging directory (unused for clipboard).
    is_rooted:   Unused.

    Returns
    -------
    List of ClipboardItem objects; empty list on failure or empty clipboard.
    """
    try:
        from core.adb_manager import ADBManager
        adb = ADBManager(get_config())
        setup_adb_forward(adb, serial)
    except Exception as exc:
        logger.error("[clipboard/android] ADB forward setup failed: %s", exc)
        return []

    try:
        with CompanionClient() as client:
            if not client.ping():
                logger.error(
                    "[clipboard/android] Companion APK not responding on %s",
                    serial,
                )
                return []

            response = client.send_recv({"cmd": "extract_clipboard"})

        if response.get("status") != "ok":
            logger.warning(
                "[clipboard/android] APK returned status '%s': %s",
                response.get("status"), response.get("message"),
            )
            return []

        raw_items: list[dict] = response.get("data", [])
        if not raw_items:
            logger.info("[clipboard/android] Clipboard is empty")
            return []

        items: list[ClipboardItem] = []
        for raw in raw_items:
            text = raw.get("text", "")
            if text:
                items.append(ClipboardItem(
                    text=text,
                    mime_type=raw.get("mime_type"),
                ))

        logger.info(
            "[clipboard/android] Extracted %d clipboard item(s) from %s",
            len(items), serial,
        )
        return items

    except Exception:
        logger.exception("[clipboard/android] Unhandled error during extraction")
        return []
    finally:
        try:
            teardown_adb_forward(adb, serial)
        except Exception:
            pass
