"""
extract_calendar_ios.py

Extracts calendar events from an iOS device and returns a list of
CalendarEvent objects defined in normalization_schema.py.

Strategy
--------
1. Pull Calendar.sqlitedb via AFC2 (jailbroken) or iOSbackup (non-jailbroken).
2. Query the CalCalendarItem table (Core Data-backed SQLite).
3. Convert Apple epoch floats (seconds since 2001-01-01) to UTC datetimes.

Never raises — all exceptions are caught, logged, and return partial/empty
results.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.normalization_schema import CalendarEvent

logger = logging.getLogger(__name__)

# Apple epoch offset
_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# Device / backup paths
_DB_DEVICE_PATH = "/var/mobile/Library/Calendar/Calendar.sqlitedb"
_DB_RELATIVE_PATH = "Library/Calendar/Calendar.sqlitedb"
_IOS_BACKUP_DOMAIN = "HomeDomain"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(udid: str, staging_dir: Path, is_jailbroken: bool = False) -> list[CalendarEvent]:
    """
    Extract all calendar events from the iOS device identified by *udid*.

    Parameters
    ----------
    udid:           iOS device UDID.
    staging_dir:    Local directory used for temporary file copies.
    is_jailbroken:  Whether the device has AFC2 available.

    Returns
    -------
    list[CalendarEvent]   Possibly empty on total failure.
    """
    try:
        return _extract_impl(udid, staging_dir, is_jailbroken)
    except Exception as exc:
        logger.exception("extract_calendar_ios: top-level failure for %s: %s", udid, exc)
        return []


def _extract_impl(udid: str, staging_dir: Path, is_jailbroken: bool) -> list[CalendarEvent]:
    work_dir = staging_dir / "calendar_ios"
    work_dir.mkdir(parents=True, exist_ok=True)

    db_path = _pull_calendar_db(udid, work_dir, is_jailbroken)
    if db_path is None:
        logger.warning("calendar_ios: could not obtain Calendar.sqlitedb for %s", udid)
        return []

    events = _parse_calendar_db(db_path)
    logger.info("calendar_ios: extracted %d events for %s", len(events), udid)
    return events


# ---------------------------------------------------------------------------
# Pull Calendar.sqlitedb
# ---------------------------------------------------------------------------

def _pull_calendar_db(udid: str, work_dir: Path, is_jailbroken: bool) -> Path | None:
    local_db = work_dir / "Calendar.sqlitedb"

    if is_jailbroken:
        try:
            from core.device_connection_cache import get_broker
            from core.afc2_connector import AFC2Connector

            broker = get_broker(udid)
            with AFC2Connector(broker) as afc2:
                ok = afc2.pull_file(_DB_DEVICE_PATH, local_db)
            if ok and local_db.exists():
                logger.debug("calendar_ios: pulled Calendar.sqlitedb via AFC2")
                return local_db
        except PermissionError:
            logger.warning("calendar_ios: AFC2 not available despite is_jailbroken=True")
        except Exception as exc:
            logger.warning("calendar_ios: AFC2 pull failed: %s", exc)

    return _pull_via_iosbackup(udid, _DB_RELATIVE_PATH, _IOS_BACKUP_DOMAIN, local_db)


def _pull_via_iosbackup(udid: str, relative_path: str, domain: str, dest: Path) -> Path | None:
    try:
        from core.device_connection_cache import get_iosbackup
        dest.parent.mkdir(parents=True, exist_ok=True)
        backup = get_iosbackup(udid)
        info = backup.getFileDecryptedCopy(
            relativePath=relative_path,
            targetName=dest.name,
            targetFolder=str(dest.parent),
        )
        if info and dest.exists():
            logger.debug("calendar_ios: pulled %s via iOSbackup", relative_path)
            return dest
    except Exception as exc:
        logger.warning("calendar_ios: iOSbackup pull failed for %s: %s", relative_path, exc)

    return None


# ---------------------------------------------------------------------------
# Timestamp conversion
# ---------------------------------------------------------------------------

def _apple_ts_to_datetime(ts: float | int | None) -> datetime | None:
    """
    Convert Apple epoch seconds/float (since 2001-01-01) to UTC datetime.
    Returns None if ts is None or zero (treat as "no date set").
    """
    if ts is None:
        return None
    ts_f = float(ts)
    if ts_f == 0.0:
        return None
    try:
        return _APPLE_EPOCH + timedelta(seconds=ts_f)
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Parse Calendar.sqlitedb
# ---------------------------------------------------------------------------

def _parse_calendar_db(db_path: Path) -> list[CalendarEvent]:
    """
    Read calendar events from Calendar.sqlitedb.

    The primary table is CalCalendarItem (Core Data entity for events).

    Column mapping (actual column names vary slightly by iOS version):
      ZSUMMARY / summary         — event title
      ZSTARTDATE / start_date    — Apple epoch float
      ZENDDATE / end_date        — Apple epoch float
      ZISALLDAY / all_day        — integer bool
      ZLOCATION / location       — text
      ZNOTES / notes             — text
      ZEXTERNALIDENTIFIER / uid  — text UID
      ZRRULE / rrule             — recurrence rule string

    We try multiple column name spellings to handle schema variations
    across iOS versions (the raw SQLite columns sometimes match the Core
    Data attribute names with a Z-prefix).
    """
    events: list[CalendarEvent] = []

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row

            # Discover the actual table name (it may have a prefix)
            table_name = _find_calendar_table(conn)
            if table_name is None:
                logger.error(
                    "calendar_ios: cannot find CalCalendarItem table in %s", db_path
                )
                return []

            # Discover column names
            col_info = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            available = {row[1].upper(): row[1] for row in col_info}

            def col(candidates: list[str]) -> str | None:
                """Return the first matching column name (case-insensitive)."""
                for c in candidates:
                    if c.upper() in available:
                        return available[c.upper()]
                return None

            c_title = col(["ZSUMMARY", "SUMMARY", "ZTITLE", "TITLE"])
            c_start = col(["ZSTARTDATE", "STARTDATE", "START_DATE"])
            c_end = col(["ZENDDATE", "ENDDATE", "END_DATE"])
            c_allday = col(["ZISALLDAY", "ISALLDAY", "ALL_DAY", "ALLDAY"])
            c_location = col(["ZLOCATION", "LOCATION"])
            c_notes = col(["ZNOTES", "NOTES"])
            c_uid = col(["ZEXTERNALIDENTIFIER", "EXTERNALIDENTIFIER", "UID", "ZUID"])
            c_rrule = col(["ZRRULE", "RRULE"])

            if c_title is None or c_start is None or c_end is None:
                logger.error(
                    "calendar_ios: required columns missing in %s (found: %s)",
                    table_name,
                    list(available.keys()),
                )
                return []

            # Build SELECT
            select_parts = [c_title, c_start, c_end]
            extras = {
                "allday": c_allday,
                "location": c_location,
                "notes": c_notes,
                "uid": c_uid,
                "rrule": c_rrule,
            }
            for k, v in extras.items():
                if v:
                    select_parts.append(v)

            query = f"SELECT {', '.join(select_parts)} FROM {table_name}"

            try:
                cur = conn.execute(query)
            except sqlite3.OperationalError as exc:
                logger.error("calendar_ios: query failed: %s", exc)
                return []

            for row in cur.fetchall():
                try:
                    event = _row_to_event(
                        row,
                        c_title=c_title,
                        c_start=c_start,
                        c_end=c_end,
                        c_allday=c_allday,
                        c_location=c_location,
                        c_notes=c_notes,
                        c_uid=c_uid,
                        c_rrule=c_rrule,
                    )
                    if event is not None:
                        events.append(event)
                except Exception as exc:
                    logger.debug("calendar_ios: skipping event row: %s", exc)

    except Exception as exc:
        logger.exception("calendar_ios: failed to parse Calendar.sqlitedb: %s", exc)

    return events


def _find_calendar_table(conn: sqlite3.Connection) -> str | None:
    """
    Find the table that holds calendar event items.  In Core Data SQLite
    files the table is typically named ZCALCALENDARITEM or CalCalendarItem.
    """
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [row[0] for row in tables]
    except Exception as exc:
        logger.error("calendar_ios: sqlite_master query failed: %s", exc)
        return None

    # Priority order of expected table names
    candidates = [
        "ZCALCALENDARITEM",
        "CalCalendarItem",
        "CALCALENDARITEM",
    ]
    for c in candidates:
        for t in table_names:
            if t.upper() == c.upper():
                return t

    # Fuzzy match: any table containing "CALENDARITEM" or "CALITEM"
    for t in table_names:
        tu = t.upper()
        if "CALENDARITEM" in tu or "CALITEM" in tu or "CALEVENT" in tu:
            return t

    logger.debug("calendar_ios: available tables: %s", table_names)
    return None


def _row_to_event(
    row: sqlite3.Row,
    *,
    c_title: str,
    c_start: str,
    c_end: str,
    c_allday: str | None,
    c_location: str | None,
    c_notes: str | None,
    c_uid: str | None,
    c_rrule: str | None,
) -> CalendarEvent | None:
    title = (row[c_title] or "").strip()
    if not title:
        title = "(Untitled)"

    start = _apple_ts_to_datetime(row[c_start])
    end = _apple_ts_to_datetime(row[c_end])

    # Skip events without dates
    if start is None:
        return None
    if end is None:
        end = start

    all_day = bool(row[c_allday]) if c_allday and row[c_allday] is not None else False
    location = (row[c_location] or "").strip() or None if c_location else None
    notes = (row[c_notes] or "").strip() or None if c_notes else None
    uid = (row[c_uid] or "").strip() or None if c_uid else None
    rrule = (row[c_rrule] or "").strip() or None if c_rrule else None

    return CalendarEvent(
        title=title,
        start=start,
        end=end,
        all_day=all_day,
        uid=uid,
        location=location,
        notes=notes,
        recurrence_rule=rrule,
    )
