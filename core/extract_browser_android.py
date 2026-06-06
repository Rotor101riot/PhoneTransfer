"""
extract_browser_android.py

Extracts browser history from an Android device.

Supported browsers (tried in order):
  1. Google Chrome  — /data/data/com.android.chrome/app_chrome/Default/History
  2. Samsung Internet — /data/data/com.sec.android.app.sbrowser/app_sbrowser/Default/History
  3. Firefox          — /data/data/org.mozilla.firefox/files/places.sqlite
  4. Edge             — /data/data/com.microsoft.emmx/app_msedge/Default/History

All paths are in protected app directories and require root access.
Non-rooted devices return [] with a guidance message.

Returns a list of BrowserHistoryEntry objects (normalization_schema.py).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.adb_manager import ADBManager
from core.config_loader import get_config
from core.normalization_schema import BrowserHistoryEntry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Browser database specs
# ---------------------------------------------------------------------------

# (browser_label, package, device_db_path, sdcard_tmp, parser_fn)
# parser_fn is resolved by name below
_BROWSERS: list[tuple[str, str, str, str]] = [
    (
        "chrome",
        "com.android.chrome",
        "/data/data/com.android.chrome/app_chrome/Default/History",
        "/sdcard/PT_chrome_hist_tmp",
    ),
    (
        "samsung",
        "com.sec.android.app.sbrowser",
        "/data/data/com.sec.android.app.sbrowser/app_sbrowser/Default/History",
        "/sdcard/PT_samsung_hist_tmp",
    ),
    (
        "edge",
        "com.microsoft.emmx",
        "/data/data/com.microsoft.emmx/app_msedge/Default/History",
        "/sdcard/PT_edge_hist_tmp",
    ),
    (
        "firefox",
        "org.mozilla.firefox",
        "/data/data/org.mozilla.firefox/files/places.sqlite",
        "/sdcard/PT_firefox_places_tmp",
    ),
]

# Chromium epoch: microseconds since 1601-01-01 00:00:00 UTC
_CHROMIUM_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
_SUBDIR = "browser_android"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(
    serial: str,
    staging_dir: Path,
    is_rooted: bool = False,
) -> list[BrowserHistoryEntry]:
    if not is_rooted:
        logger.warning(
            "[browser/android] Browser history extraction requires root access. "
            "Browser history databases reside in protected app directories "
            "(/data/data/<package>/) that are inaccessible without root."
        )
        return []

    try:
        return _extract_impl(serial, staging_dir)
    except Exception:
        logger.exception("[browser/android] Unhandled error during extraction")
        return []


def _extract_impl(serial: str, staging_dir: Path) -> list[BrowserHistoryEntry]:
    sub = staging_dir / _SUBDIR
    sub.mkdir(parents=True, exist_ok=True)

    adb = ADBManager(get_config())
    all_entries: list[BrowserHistoryEntry] = []

    for browser_label, _pkg, device_path, sdcard_tmp in _BROWSERS:
        local_db = sub / f"{browser_label}_history.db"
        if not _pull_private_db(serial, device_path, sdcard_tmp, local_db, adb):
            continue

        if browser_label == "firefox":
            entries = _parse_firefox(local_db, browser_label)
        else:
            entries = _parse_chromium(local_db, browser_label)

        if entries:
            logger.info(
                "[browser/android] %s: %d entries", browser_label, len(entries)
            )
            all_entries.extend(entries)

    if all_entries:
        logger.info("[browser/android] Total: %d entries from all browsers", len(all_entries))
    else:
        logger.warning(
            "[browser/android] No browser history found on %s. "
            "Checked Chrome, Samsung Internet, Edge, and Firefox.",
            serial,
        )

    return all_entries


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_chromium(db_path: Path, browser: str) -> list[BrowserHistoryEntry]:
    """
    Parse a Chromium-based browser's History SQLite database.

    Tables used:
      urls    — url, title, visit_count, last_visit_time (Chromium microseconds)
      visits  — visit_time (optional; we use urls.last_visit_time for simplicity)
    """
    entries = []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT url, title, visit_count, last_visit_time "
                "FROM urls ORDER BY last_visit_time DESC"
            )
            for row in cur.fetchall():
                entries.append(BrowserHistoryEntry(
                    url         = row["url"] or "",
                    title       = row["title"] or "",
                    visited     = _chromium_ts(row["last_visit_time"]),
                    visit_count = int(row["visit_count"] or 1),
                    browser     = browser,
                ))
    except Exception as exc:
        logger.debug("[browser/android] %s parse error: %s", browser, exc)

    return entries


def _parse_firefox(db_path: Path, browser: str) -> list[BrowserHistoryEntry]:
    """
    Parse Firefox's places.sqlite database.

    Tables used:
      moz_places  — url, title, visit_count, last_visit_date (microseconds since Unix epoch)
    """
    entries = []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT url, title, visit_count, last_visit_date "
                "FROM moz_places "
                "WHERE visit_count > 0 AND url NOT LIKE 'place:%' "
                "ORDER BY last_visit_date DESC"
            )
            for row in cur.fetchall():
                entries.append(BrowserHistoryEntry(
                    url         = row["url"] or "",
                    title       = row["title"] or "",
                    visited     = _firefox_ts(row["last_visit_date"]),
                    visit_count = int(row["visit_count"] or 1),
                    browser     = browser,
                ))
    except Exception as exc:
        logger.debug("[browser/android] firefox parse error: %s", exc)

    return entries


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pull_private_db(
    serial: str,
    device_path: str,
    sdcard_tmp: str,
    local_path: Path,
    adb: ADBManager,
) -> bool:
    _, _, rc = adb.shell_root(
        serial,
        f"cp {device_path} {sdcard_tmp} && chmod 644 {sdcard_tmp}",
        timeout=20,
    )
    if rc != 0:
        logger.debug("[browser/android] root-cp failed for %s (rc=%d)", device_path, rc)
        return False

    ok = adb.pull(serial, sdcard_tmp, local_path, timeout=60)
    adb.shell(serial, f"rm -f {sdcard_tmp}", timeout=10)

    return ok and local_path.exists() and local_path.stat().st_size > 0


def _chromium_ts(us: int | None) -> datetime:
    """Convert Chromium timestamp (μs since 1601-01-01) to UTC datetime."""
    if not us:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        return _CHROMIUM_EPOCH + timedelta(microseconds=int(us))
    except (OverflowError, OSError, ValueError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _firefox_ts(us: int | None) -> datetime:
    """Convert Firefox timestamp (μs since Unix epoch) to UTC datetime."""
    if not us:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        return datetime.utcfromtimestamp(int(us) / 1_000_000).replace(tzinfo=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
