"""
extract_calendar_android.py

Extracts calendar events from an Android device connected via ADB and returns
a list of CalendarEvent objects defined in normalization_schema.py.

Strategy
--------
Non-rooted path:
    Query the CalendarContract content provider via
    `adb shell content query --uri content://com.android.calendar/events`.
    Falls back to the older `content://calendar/events` URI on failure.

Rooted path (additional attempt):
    Copy calendar.db directly from
    /data/data/com.android.providers.calendar/databases/calendar.db,
    pull it to staging, and parse via sqlite3.  Results are merged with any
    records obtained from the content provider (deduplication by _id).

Timestamp handling:
    The CalendarContract stores dtstart / dtend as milliseconds since the
    Unix epoch (UTC).  These are converted to timezone-aware UTC datetimes.

Output format filtering:
    Rows with deleted=1 are excluded from results.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.normalization_schema import CalendarEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_URI_EVENTS_PRIMARY = "content://com.android.calendar/events"
_URI_EVENTS_LEGACY  = "content://calendar/events"

_PROJECTION = (
    "_id:title:description:dtstart:dtend:allDay:eventLocation:rrule:deleted"
)

_REMOTE_DB      = "/data/data/com.android.providers.calendar/databases/calendar.db"
_REMOTE_TMP     = "/sdcard/calendar_tmp.db"
_LOCAL_DB_NAME  = "calendar.db"
_SUBDIR         = "calendar_android"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(device_id: str, staging_dir: Path, is_rooted: bool) -> list[CalendarEvent]:
    """
    Extract all calendar events from the Android device identified by *device_id*.

    Parameters
    ----------
    device_id:   ADB device serial string.
    staging_dir: Root staging directory for this transfer session.
    is_rooted:   If True, attempt direct DB pull in addition to content provider.

    Returns
    -------
    list[CalendarEvent]   Possibly empty on total failure.
    """
    try:
        return _extract_impl(device_id, staging_dir, is_rooted)
    except Exception as exc:
        logger.exception(
            "[calendar/android] Top-level failure for %s: %s", device_id, exc
        )
        return []


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------

def _extract_impl(
    device_id: str, staging_dir: Path, is_rooted: bool
) -> list[CalendarEvent]:
    from core.adb_manager import ADBManager
    from core.config_loader import get_config

    sub = staging_dir / _SUBDIR
    sub.mkdir(parents=True, exist_ok=True)

    adb = ADBManager(get_config())

    # ------------------------------------------------------------------
    # Step 1: query content provider (available without root)
    # ------------------------------------------------------------------
    events_by_id: dict[str, CalendarEvent] = {}

    provider_rows = _query_content_provider(device_id, adb)
    for row in provider_rows:
        if row.get("deleted", "0") == "1":
            continue
        event = _row_to_event(row)
        if event is not None:
            events_by_id[row.get("_id", "")] = event

    logger.info(
        "[calendar/android] Content provider: %d event(s) for %s",
        len(events_by_id),
        device_id,
    )

    # ------------------------------------------------------------------
    # Step 2: rooted path — direct SQLite access (merges / fills gaps)
    # ------------------------------------------------------------------
    if is_rooted:
        db_events = _extract_rooted(device_id, sub, adb)
        if db_events is not None:
            before = len(events_by_id)
            for ev_id, ev in db_events.items():
                if ev_id not in events_by_id:
                    events_by_id[ev_id] = ev
            logger.info(
                "[calendar/android] Rooted DB added %d additional event(s)",
                len(events_by_id) - before,
            )
        else:
            logger.warning(
                "[calendar/android] Rooted DB path failed for %s", device_id
            )

    result = list(events_by_id.values())
    logger.info(
        "[calendar/android] Total extracted: %d event(s) for %s",
        len(result),
        device_id,
    )
    return result


# ---------------------------------------------------------------------------
# Content provider query
# ---------------------------------------------------------------------------

def _query_content_provider(
    device_id: str, adb  # ADBManager — typed loosely to avoid circular import
) -> list[dict[str, str]]:
    """Try both known CalendarContract URIs and return parsed rows."""
    for uri in (_URI_EVENTS_PRIMARY, _URI_EVENTS_LEGACY):
        stdout, stderr, rc = adb.shell(
            device_id,
            f"content query --uri {uri} --projection {_PROJECTION}",
            timeout=60,
        )
        if rc == 0 and stdout.strip():
            rows = _parse_content_rows(stdout)
            if rows:
                logger.debug(
                    "[calendar/android] content query succeeded with URI %s (%d rows)",
                    uri,
                    len(rows),
                )
                return rows
        else:
            logger.debug(
                "[calendar/android] content query failed for %s (rc=%d): %s",
                uri,
                rc,
                stderr.strip(),
            )

    logger.warning(
        "[calendar/android] Both CalendarContract URIs returned no rows for %s",
        device_id,
    )
    return []


def _parse_content_rows(output: str) -> list[dict[str, str]]:
    """
    Parse the stdout of `adb shell content query` into a list of dicts.

    Line format example:
        Row: 0 _id=1, title=Birthday, description=My desc, dtstart=1700000000000, ...

    Values may contain commas (e.g. in descriptions), so we split only at
    ", <word>=" boundaries — specifically where the separator is followed
    by a known identifier character.
    """
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("Row:"):
            continue
        # Drop "Row:" prefix and the row-index token
        _, _, rest = line.partition(" ")   # remove "Row:"
        _, _, rest = rest.partition(" ")   # remove index number
        rest = rest.strip()
        if not rest:
            continue
        # Split at ", key=" boundaries where key starts with a word char
        pairs = re.split(r",\s+(?=\w+=)", rest)
        row: dict[str, str] = {}
        for pair in pairs:
            k, _, v = pair.partition("=")
            row[k.strip()] = v.strip()
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Rooted path — direct SQLite access
# ---------------------------------------------------------------------------

def _extract_rooted(
    device_id: str, sub: Path, adb
) -> dict[str, CalendarEvent] | None:
    """
    Copy calendar.db off the device, pull to staging, parse locally.
    Returns None on any failure so the caller treats it as a graceful miss.
    """
    local_db = sub / _LOCAL_DB_NAME

    _, _, rc = adb.shell_root(
        device_id,
        f"cp {_REMOTE_DB} {_REMOTE_TMP}",
        timeout=30,
    )
    if rc != 0:
        logger.warning("[calendar/android] su cp failed (rc=%d)", rc)
        return None

    adb.shell_root(device_id, f"chmod 644 {_REMOTE_TMP}", timeout=10)
    pulled = adb.pull_verified(device_id, _REMOTE_TMP, local_db, timeout=60)
    adb.shell(device_id, f"rm -f {_REMOTE_TMP}", timeout=10)

    if not pulled or not local_db.exists():
        logger.warning("[calendar/android] adb pull of calendar.db failed")
        return None

    try:
        return _parse_sqlite_calendar(local_db)
    except Exception as exc:
        logger.exception("[calendar/android] SQLite parse error: %s", exc)
        return None


def _parse_sqlite_calendar(db_path: Path) -> dict[str, CalendarEvent]:
    """
    Parse calendar.db (CalendarContract backing store) directly.
    Returns a dict of { _id_str: CalendarEvent }.
    """
    events: dict[str, CalendarEvent] = {}
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            # The events table is 'Events' or 'events'; discover it.
            tables = {
                r[0].lower()
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            tbl = "Events" if "events" in tables else None
            if tbl is None:
                logger.warning(
                    "[calendar/android] No events table in %s (tables: %s)",
                    db_path,
                    tables,
                )
                return {}
            # Discover which columns are actually present
            available = {
                r[1].lower(): r[1]
                for r in conn.execute("PRAGMA table_info(Events)")
            }

            def _col(*candidates: str) -> str | None:
                for c in candidates:
                    if c.lower() in available:
                        return available[c.lower()]
                return None

            c_id       = _col("_id")
            c_title    = _col("title")
            c_desc     = _col("description")
            c_dtstart  = _col("dtstart")
            c_dtend    = _col("dtend")
            c_allday   = _col("allday", "allDay")
            c_location = _col("eventlocation", "eventLocation", "location")
            c_rrule    = _col("rrule")
            c_deleted  = _col("deleted")

            if not all([c_id, c_title, c_dtstart, c_dtend]):
                logger.warning(
                    "[calendar/android] Required columns missing in calendar.db"
                )
                return {}

            select_cols = [
                c for c in [
                    c_id, c_title, c_desc, c_dtstart, c_dtend,
                    c_allday, c_location, c_rrule, c_deleted,
                ]
                if c is not None
            ]

            cursor = conn.execute(
                f"SELECT {', '.join(select_cols)} FROM Events"
            )
            for row in cursor:
                try:
                    if c_deleted and row[c_deleted] == 1:
                        continue
                    row_dict: dict[str, str] = {}
                    if c_id:
                        row_dict["_id"] = str(row[c_id] or "")
                    if c_title:
                        row_dict["title"] = str(row[c_title] or "")
                    if c_desc:
                        row_dict["description"] = str(row[c_desc] or "")
                    if c_dtstart:
                        row_dict["dtstart"] = str(row[c_dtstart] or "0")
                    if c_dtend:
                        row_dict["dtend"] = str(row[c_dtend] or "0")
                    if c_allday:
                        row_dict["allDay"] = str(row[c_allday] or "0")
                    if c_location:
                        row_dict["eventLocation"] = str(row[c_location] or "")
                    if c_rrule:
                        row_dict["rrule"] = str(row[c_rrule] or "")

                    event = _row_to_event(row_dict)
                    if event is not None:
                        events[row_dict.get("_id", "")] = event
                except Exception as exc:
                    logger.debug(
                        "[calendar/android] Skipping DB row: %s", exc
                    )
    except Exception as exc:
        logger.exception(
            "[calendar/android] Failed to open calendar.db: %s", exc
        )
    return events


# ---------------------------------------------------------------------------
# Row → CalendarEvent conversion
# ---------------------------------------------------------------------------

def _ms_to_utc(ms_str: str) -> datetime | None:
    """Convert a Unix milliseconds string to a UTC-aware datetime."""
    try:
        ms = int(ms_str)
        if ms == 0:
            return None
        return datetime.utcfromtimestamp(ms / 1000.0).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _row_to_event(row: dict[str, str]) -> CalendarEvent | None:
    """
    Convert a content-row dict (string values) to a CalendarEvent.
    Returns None if the row is missing essential date data.
    """
    title = (row.get("title") or "").strip() or "(Untitled)"

    start = _ms_to_utc(row.get("dtstart", "0"))
    if start is None:
        logger.debug(
            "[calendar/android] Skipping event %r — no valid dtstart",
            title,
        )
        return None

    end = _ms_to_utc(row.get("dtend", "0"))
    if end is None:
        end = start  # zero-duration fallback

    all_day_raw = (row.get("allDay") or row.get("allday") or "0").strip()
    all_day = all_day_raw not in ("0", "false", "", "null", "NULL")

    location_raw = (
        row.get("eventLocation") or row.get("eventlocation") or ""
    ).strip()
    location = location_raw or None

    notes_raw = (row.get("description") or "").strip()
    notes = notes_raw or None

    rrule_raw = (row.get("rrule") or "").strip()
    rrule = rrule_raw or None

    return CalendarEvent(
        title=title,
        start=start,
        end=end,
        all_day=all_day,
        uid=None,           # CalendarContract does not expose a stable UID
        location=location,
        notes=notes,
        recurrence_rule=rrule,
    )
