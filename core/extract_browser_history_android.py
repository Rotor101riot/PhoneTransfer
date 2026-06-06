"""
extract_browser_history_android.py

Extracts browser history from an Android device via the companion APK's
``extract_browser_history`` command.

This is the companion-app-based path that works without root access by
querying the Chrome content provider from within the companion APK.
Falls back to the existing root-based extraction (extract_browser_android)
if the companion is not available.

The companion APK returns a JSON array of entries with: id, title, url,
visit_count, last_visited (epoch milliseconds).

Returns a list of BrowserHistoryEntry objects (normalization_schema.py).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from core.companion_app_protocol import CompanionClient, setup_adb_forward, teardown_adb_forward
from core.config_loader import get_config
from core.normalization_schema import BrowserHistoryEntry

logger = logging.getLogger(__name__)


def _epoch_ms_to_datetime(ms: int | float | None) -> datetime:
    """Convert epoch milliseconds to a timezone-aware UTC datetime."""
    if not ms:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def extract(
    serial: str,
    staging_dir: Path,
    is_rooted: bool = False,
) -> list[BrowserHistoryEntry]:
    """
    Extract browser history from the Android device via the companion APK.

    Unlike the root-based extract_browser_android module, this path uses the
    companion APK's ``extract_browser_history`` command which queries browser
    content providers from within the app context (no root required, though
    results may be limited by provider access restrictions).

    Parameters
    ----------
    serial:      ADB device serial string.
    staging_dir: Root staging directory (unused for browser history).
    is_rooted:   If True and companion extraction returns empty, falls back
                 to the existing root-based extraction path.

    Returns
    -------
    List of BrowserHistoryEntry objects; empty list on failure.
    """
    try:
        from core.adb_manager import ADBManager
        adb = ADBManager(get_config())
        setup_adb_forward(adb, serial)
    except Exception as exc:
        logger.error("[browser_history/android] ADB forward setup failed: %s", exc)
        return _fallback_root(serial, staging_dir, is_rooted)

    try:
        with CompanionClient() as client:
            if not client.ping():
                logger.warning(
                    "[browser_history/android] Companion APK not responding — "
                    "falling back to root path"
                )
                return _fallback_root(serial, staging_dir, is_rooted)

            response = client.send_recv({"cmd": "extract_browser_history"})

        if response.get("status") != "ok":
            logger.warning(
                "[browser_history/android] APK returned status '%s': %s",
                response.get("status"), response.get("message"),
            )
            return _fallback_root(serial, staging_dir, is_rooted)

        raw_items: list[dict] = response.get("data", [])
        if not raw_items:
            logger.info(
                "[browser_history/android] Companion returned 0 entries; "
                "trying root fallback"
            )
            return _fallback_root(serial, staging_dir, is_rooted)

        entries: list[BrowserHistoryEntry] = []
        for raw in raw_items:
            url = raw.get("url", "")
            if not url:
                continue
            entries.append(BrowserHistoryEntry(
                url=url,
                title=raw.get("title", ""),
                visited=_epoch_ms_to_datetime(raw.get("last_visited")),
                visit_count=int(raw.get("visit_count", 1)),
                browser="chrome",
            ))

        logger.info(
            "[browser_history/android] Extracted %d browser history entries "
            "via companion from %s",
            len(entries), serial,
        )
        return entries

    except Exception:
        logger.exception("[browser_history/android] Unhandled error during extraction")
        return _fallback_root(serial, staging_dir, is_rooted)
    finally:
        try:
            teardown_adb_forward(adb, serial)
        except Exception:
            pass


def _fallback_root(
    serial: str,
    staging_dir: Path,
    is_rooted: bool,
) -> list[BrowserHistoryEntry]:
    """
    Fall back to the existing root-based browser extraction if available.
    """
    if not is_rooted:
        logger.info(
            "[browser_history/android] No companion data and device is not "
            "rooted — cannot extract browser history"
        )
        return []

    try:
        from core.extract_browser_android import extract as root_extract
        logger.info(
            "[browser_history/android] Falling back to root-based extraction"
        )
        return root_extract(serial, staging_dir, is_rooted=True)
    except ImportError:
        logger.debug(
            "[browser_history/android] Root-based extract_browser_android "
            "module not available"
        )
        return []
    except Exception as exc:
        logger.error(
            "[browser_history/android] Root fallback failed: %s", exc
        )
        return []
