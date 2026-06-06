"""
extract_browser_ios.py

Extracts Safari browser history from an iOS device.

Safari stores history in:
  /var/mobile/Library/Safari/History.db  (jailbroken via AFC2)
  HomeDomain/Library/Safari/History.db   (iOSbackup path)

Tables:
  history_items  — id, url, title, visit_count, domain_expansion
  history_visits — id, history_item (FK), visit_time (Apple epoch)

Returns a list of BrowserHistoryEntry objects.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.normalization_schema import BrowserHistoryEntry

logger = logging.getLogger(__name__)

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

_SAFARI_DB_PATH     = "/var/mobile/Library/Safari/History.db"
_IOSBACKUP_DOMAIN   = "HomeDomain"
_IOSBACKUP_RELATIVE = "Library/Safari/History.db"

_SUBDIR = "browser_ios"


def extract(
    udid: str,
    staging_dir: Path,
    is_jailbroken: bool = False,
) -> list[BrowserHistoryEntry]:
    try:
        return _extract_impl(udid, staging_dir, is_jailbroken)
    except Exception:
        logger.exception("[browser/ios] Unhandled error during extraction")
        return []


def _extract_impl(
    udid: str,
    staging_dir: Path,
    is_jailbroken: bool,
) -> list[BrowserHistoryEntry]:
    sub = staging_dir / _SUBDIR
    sub.mkdir(parents=True, exist_ok=True)

    db_path = _obtain_db(udid, sub, is_jailbroken)
    if db_path is None:
        logger.warning(
            "[browser/ios] Could not obtain Safari History.db for %s. "
            "Jailbroken devices need AFC2; non-jailbroken devices need a local "
            "iTunes/Finder backup.",
            udid,
        )
        return []

    entries = _parse_history_db(db_path)
    logger.info("[browser/ios] Extracted %d Safari history entries for %s", len(entries), udid)
    return entries


# ---------------------------------------------------------------------------
# Obtain History.db
# ---------------------------------------------------------------------------

def _obtain_db(udid: str, sub: Path, is_jailbroken: bool) -> Path | None:
    dest = sub / "safari_history.db"

    if is_jailbroken:
        result = _pull_via_afc(udid, dest)
        if result is not None:
            return result
        logger.warning("[browser/ios] AFC pull failed; trying iOSbackup")

    return _pull_via_iosbackup(udid, dest)


def _pull_via_afc(udid: str, dest: Path) -> Path | None:
    """Pull via standard AFC (HomeDomain is accessible without AFC2)."""
    try:
        from core.afc_connector import AFCConnector
    except ImportError:
        logger.debug("[browser/ios] AFCConnector not available")
        return None

    try:
        with AFCConnector(udid) as afc:
            data = afc.read_file(_SAFARI_DB_PATH)
        if data and len(data) > 100:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            logger.debug("[browser/ios] Pulled Safari History.db via AFC")
            return dest
    except Exception as exc:
        logger.debug("[browser/ios] AFC read failed: %s", exc)

    return None


def _pull_via_iosbackup(udid: str, dest: Path) -> Path | None:
    try:
        from core.device_connection_cache import get_iosbackup
        dest.parent.mkdir(parents=True, exist_ok=True)
        backup = get_iosbackup(udid)
        info = backup.getFileDecryptedCopy(
            relativePath=_IOSBACKUP_RELATIVE,
            targetName=dest.name,
            targetFolder=str(dest.parent),
        )
        if not info or not dest.exists():
            logger.warning(
                "[browser/ios] iOSbackup returned no data for Safari History on %s", udid
            )
            return None

        logger.debug("[browser/ios] Pulled Safari History.db via iOSbackup")
        return dest
    except Exception as exc:
        logger.warning("[browser/ios] iOSbackup pull failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Parse History.db
# ---------------------------------------------------------------------------

def _parse_history_db(db_path: Path) -> list[BrowserHistoryEntry]:
    entries = []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row

            # Detect whether title lives in history_items (pre-iOS 18) or
            # history_visits (iOS 18+, where it was moved to the visits table).
            try:
                hi_cols = {r[1] for r in conn.execute("PRAGMA table_info(history_items)").fetchall()}
                hv_cols = {r[1] for r in conn.execute("PRAGMA table_info(history_visits)").fetchall()}
            except sqlite3.OperationalError as exc:
                logger.warning("[browser/ios] PRAGMA query failed: %s", exc)
                return []

            title_in_items = "title" in hi_cols
            title_in_visits = "title" in hv_cols

            # Build a map of history_item id → (url, visit_count)
            items: dict[int, dict] = {}
            hi_select = "id, url, visit_count" + (", title" if title_in_items else "")
            try:
                cur = conn.execute(f"SELECT {hi_select} FROM history_items")
                for row in cur.fetchall():
                    items[row["id"]] = {
                        "url":         row["url"] or "",
                        "title":       (row["title"] if title_in_items else "") or "",
                        "visit_count": int(row["visit_count"] or 1),
                    }
            except sqlite3.OperationalError as exc:
                logger.warning("[browser/ios] history_items query failed: %s", exc)
                return []

            # For each visit pick the most recent visit_time and (if iOS 18+) title
            latest_visit: dict[int, float] = {}
            latest_title: dict[int, str] = {}
            hv_select = "history_item, visit_time" + (", title" if title_in_visits else "")
            try:
                cur = conn.execute(
                    f"SELECT {hv_select} FROM history_visits ORDER BY visit_time DESC"
                )
                for row in cur.fetchall():
                    item_id = row["history_item"]
                    vt = float(row["visit_time"] or 0)
                    if item_id not in latest_visit:
                        latest_visit[item_id] = vt
                    if title_in_visits and item_id not in latest_title:
                        latest_title[item_id] = row["title"] or ""
            except sqlite3.OperationalError as exc:
                logger.debug("[browser/ios] history_visits query failed: %s", exc)

            for item_id, meta in items.items():
                vt = latest_visit.get(item_id, 0)
                title = latest_title.get(item_id, meta["title"]) if title_in_visits else meta["title"]
                entries.append(BrowserHistoryEntry(
                    url         = meta["url"],
                    title       = title,
                    visited     = _apple_ts(vt),
                    visit_count = meta["visit_count"],
                    browser     = "safari",
                ))

    except Exception:
        logger.exception("[browser/ios] Failed to parse Safari History.db")

    return entries


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apple_ts(ts: float | int | None) -> datetime:
    if not ts:
        return _APPLE_EPOCH
    try:
        return _APPLE_EPOCH + timedelta(seconds=float(ts))
    except (OverflowError, OSError, ValueError):
        return _APPLE_EPOCH
