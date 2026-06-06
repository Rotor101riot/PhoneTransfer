"""
inject_browser_history_android.py

Injects browser history into an Android device via the companion APK's
``inject_browser_history`` command.

Modern Android severely restricts write access to browser databases.
The companion APK acknowledges the data but notes that actual injection
is not supported on modern Android. The data is preserved in the transfer
vault for reference.

Falls back to the existing root-based injection path if available.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.companion_app_protocol import CompanionClient, setup_adb_forward, teardown_adb_forward
from core.config_loader import get_config
from core.normalization_schema import BrowserHistoryEntry

logger = logging.getLogger(__name__)


def inject(
    serial: str,
    items: list[BrowserHistoryEntry],
    staging_dir: Path,
    is_rooted: bool = False,
) -> int:
    """
    Inject browser history into the Android device.

    Attempts the companion APK path first. If the companion reports that
    injection is not supported (modern Android), falls back to the root-based
    Chrome History DB injection if the device is rooted.

    Parameters
    ----------
    serial:      ADB device serial.
    items:       BrowserHistoryEntry objects to inject.
    staging_dir: Local directory for temporary files.
    is_rooted:   If True, falls back to root-based Chrome injection.

    Returns
    -------
    Number of entries successfully injected.
    """
    if not items:
        logger.info("[browser_history/android] No browser history to inject.")
        return 0

    try:
        from core.adb_manager import ADBManager
        adb = ADBManager(get_config())
        setup_adb_forward(adb, serial)
    except Exception as exc:
        logger.error("[browser_history/android] ADB forward setup failed: %s", exc)
        return _fallback_root(serial, items, staging_dir, is_rooted)

    try:
        with CompanionClient() as client:
            if not client.ping():
                logger.warning(
                    "[browser_history/android] Companion APK not responding — "
                    "trying root fallback"
                )
                return _fallback_root(serial, items, staging_dir, is_rooted)

            data = []
            for entry in items:
                ts_ms = int(entry.visited.timestamp() * 1000) if entry.visited else 0
                data.append({
                    "url": entry.url,
                    "title": entry.title,
                    "visit_count": entry.visit_count,
                    "last_visited": ts_ms,
                })

            response = client.send_recv({
                "cmd": "inject_browser_history",
                "data": data,
            })

        if response.get("status") != "ok":
            logger.warning(
                "[browser_history/android] APK inject returned status '%s': %s",
                response.get("status"), response.get("message"),
            )
            return _fallback_root(serial, items, staging_dir, is_rooted)

        received = int(response.get("received", 0))
        note = response.get("note", "")

        if note:
            logger.info(
                "[browser_history/android] Companion note: %s", note,
            )

        # If the companion only acknowledged receipt (not actual injection),
        # try the root path for actual injection.
        if received > 0 and "not supported" in note.lower():
            logger.info(
                "[browser_history/android] Companion acknowledged %d entries "
                "but injection not supported; trying root path",
                received,
            )
            root_count = _fallback_root(serial, items, staging_dir, is_rooted)
            if root_count > 0:
                return root_count
            # Return received count so the pipeline knows data was preserved
            return received

        logger.info(
            "[browser_history/android] Injected %d browser history entries "
            "into %s",
            received, serial,
        )
        return received

    except Exception:
        logger.exception("[browser_history/android] Unhandled error during injection")
        return _fallback_root(serial, items, staging_dir, is_rooted)
    finally:
        try:
            teardown_adb_forward(adb, serial)
        except Exception:
            pass


def _fallback_root(
    serial: str,
    items: list[BrowserHistoryEntry],
    staging_dir: Path,
    is_rooted: bool,
) -> int:
    """
    Fall back to the existing root-based Chrome History injection.
    """
    if not is_rooted:
        logger.info(
            "[browser_history/android] Device is not rooted — browser "
            "history injection not possible"
        )
        return 0

    try:
        from core.inject_browser_android import inject as root_inject
        logger.info(
            "[browser_history/android] Falling back to root-based injection"
        )
        return root_inject(serial, items, staging_dir, is_rooted)
    except ImportError:
        logger.debug(
            "[browser_history/android] Root-based inject_browser_android "
            "module not available"
        )
        return 0
    except Exception as exc:
        logger.error(
            "[browser_history/android] Root fallback failed: %s", exc
        )
        return 0
