"""
inject_browser_ios.py

Inject Safari browser-history entries into iOS.

Two paths:
  * **Backup-mod (default)**: when an :class:`IOSBackupInjector` session is
    active, stage ``HomeDomain:Library/Safari/History.db`` and INSERT new
    rows into ``history_items`` + ``history_visits``.  The repacker re-
    encrypts the modified DB on commit, and Safari picks up the new rows
    after the restore + first launch.
  * **AFC2 (legacy)**: pull/push History.db through AFC2 on a jailbroken
    device.  Kept as a fallback for the rare case where the caller
    intentionally bypassed the backup-mod orchestrator.

Non-jailbroken without a backup session: HomeDomain is not writable via
standard AFC, so we log guidance and return 0.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.ios_backup_injector import IOSBackupInjector, get_current_injector
from core.normalization_schema import BrowserHistoryEntry

logger = logging.getLogger(__name__)

_HISTORY_DOMAIN = "HomeDomain"
_HISTORY_RELPATH = "Library/Safari/History.db"
_AFC2_HISTORY_PATH = "/var/mobile/Library/Safari/History.db"

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def inject(
    device_id: str,
    items: list[BrowserHistoryEntry],
    staging_dir: Path,
    is_privileged: bool,
) -> int:
    if not items:
        return 0

    injector = get_current_injector()
    if injector is not None:
        try:
            count = _inject_via_backup(injector, items)
            logger.info(
                "inject_browser_ios: staged %d history entry(ies) into the "
                "backup for %s", count, device_id,
            )
            return count
        except Exception as exc:
            logger.warning(
                "inject_browser_ios: backup-mod path failed (%s) — "
                "falling back to AFC2", exc,
            )

    if not is_privileged:
        logger.warning(
            "[browser/ios] Safari history injection requires a jailbroken "
            "device or an active backup-mod session. HomeDomain is not "
            "writable via standard AFC. %d entries are preserved in the "
            "vault for future use.", len(items),
        )
        return 0

    try:
        return _inject_jailbroken(device_id, items, staging_dir)
    except Exception:
        logger.exception("[browser/ios] Unhandled error during injection")
        return 0


# ---------------------------------------------------------------------------
# Backup-mod path
# ---------------------------------------------------------------------------

def _inject_via_backup(
    injector: IOSBackupInjector, items: list[BrowserHistoryEntry]
) -> int:
    db_path = injector.stage_db(_HISTORY_DOMAIN, _HISTORY_RELPATH)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=FULL")
        return _insert_entries(con, items)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Jailbroken AFC2 path
# ---------------------------------------------------------------------------

def _inject_jailbroken(
    device_id: str,
    items: list[BrowserHistoryEntry],
    staging_dir: Path,
) -> int:
    try:
        from core.afc2_connector import AFC2Connector
    except ImportError:
        logger.error("[browser/ios] AFC2Connector not available")
        return 0

    staging_dir.mkdir(parents=True, exist_ok=True)
    local_db = staging_dir / "safari_history_inject.db"

    try:
        with AFC2Connector(device_id) as afc2:
            data = afc2.read_file(_AFC2_HISTORY_PATH)
        if not data or len(data) < 100:
            logger.error(
                "[browser/ios] History.db is empty or missing on %s. "
                "Open Safari at least once to create the database.",
                device_id,
            )
            return 0
        local_db.write_bytes(data)
    except Exception as exc:
        logger.error("[browser/ios] Failed to pull History.db: %s", exc)
        return 0

    con = sqlite3.connect(str(local_db))
    try:
        inserted = _insert_entries(con, items)
    finally:
        con.close()
    if inserted == 0:
        return 0

    try:
        with AFC2Connector(device_id) as afc2:
            afc2.write_file(_AFC2_HISTORY_PATH, local_db.read_bytes())
    except Exception as exc:
        logger.error("[browser/ios] Failed to write History.db back: %s", exc)
        return 0

    logger.info(
        "[browser/ios] Injected %d Safari history entries into %s. "
        "Force-quit Safari to load the new history.",
        inserted, device_id,
    )
    return inserted


# ---------------------------------------------------------------------------
# Shared INSERT logic
# ---------------------------------------------------------------------------

# 8-byte counter blob: one little-endian int32 = visit count, then a 4-byte
# zero pad. Matches the minimum Safari accepts for daily_visit_counts when
# `should_recompute_derived_visit_counts=1` is set (Safari rebuilds on next
# launch).
def _counter_blob(visit_count: int) -> bytes:
    return visit_count.to_bytes(4, "little", signed=False) + b"\x00\x00\x00\x00"


def _insert_entries(con: sqlite3.Connection, items: list[BrowserHistoryEntry]) -> int:
    con.row_factory = sqlite3.Row
    inserted = 0

    row = con.execute("SELECT MAX(id) AS mx FROM history_items").fetchone()
    next_item_id = (row["mx"] or 0) + 1
    row = con.execute("SELECT MAX(id) AS mx FROM history_visits").fetchone()
    next_visit_id = (row["mx"] or 0) + 1

    existing_urls: set[str] = set()
    for row in con.execute("SELECT url FROM history_items"):
        if row["url"]:
            existing_urls.add(row["url"])

    with con:
        for entry in items:
            if not entry.url or entry.url in existing_urls:
                continue
            vt = _to_apple_epoch(entry.visited)
            vc = max(1, int(entry.visit_count or 1))

            try:
                con.execute(
                    "INSERT INTO history_items "
                    "(id, url, domain_expansion, visit_count, "
                    " daily_visit_counts, weekly_visit_counts, "
                    " autocomplete_triggers, "
                    " should_recompute_derived_visit_counts, "
                    " visit_count_score, status_code) "
                    "VALUES (?, ?, NULL, ?, ?, NULL, NULL, 1, ?, 0)",
                    (next_item_id, entry.url, vc, _counter_blob(vc), vc * 100),
                )
                con.execute(
                    "INSERT INTO history_visits "
                    "(id, history_item, visit_time, title, "
                    " load_successful, http_non_get, synthesized, "
                    " redirect_source, redirect_destination, "
                    " origin, generation, attributes, score) "
                    "VALUES (?, ?, ?, ?, 1, 0, 0, NULL, NULL, 0, 0, 0, 100)",
                    (next_visit_id, next_item_id, vt, entry.title or ""),
                )
                existing_urls.add(entry.url)
                next_item_id += 1
                next_visit_id += 1
                inserted += 1
            except Exception as exc:
                logger.debug("[browser/ios] Skipping %s: %s", entry.url[:60], exc)

    return inserted


def _to_apple_epoch(dt: datetime) -> float:
    if dt is None:
        return (datetime.now(timezone.utc) - _APPLE_EPOCH).total_seconds()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - _APPLE_EPOCH).total_seconds()
