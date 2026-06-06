"""
inject_calendar_ios.py

Injects CalendarEvent records into an iOS device connected via USB.

Strategy
--------
All events are serialised to the iCalendar format (RFC 5545) and written as
a single .ics file.

Non-jailbroken path (standard AFC):
    The .ics is pushed to /var/mobile/Media/PhoneTransfer/ via the standard
    AFC service.  The user then opens the file on the device — iOS will
    present the Calendar app import sheet.

Jailbroken path:
    Writing iCalendar data directly into the Calendar app's SQLite store is
    fragile and schema-dependent.  We therefore use the same AFC Media push
    on jailbroken devices.  AFC2 offers no meaningful advantage here, so the
    is_jailbroken flag is accepted for interface consistency only.

Return value: count of events included in the pushed .ics file.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.afc_connector import AFCConnector
from core.ios_backup_injector import IOSBackupInjector, get_current_injector
from core.ios_service_broker import IOSServiceBroker
from core.normalization_schema import CalendarEvent

logger = logging.getLogger(__name__)

# Remote directory accessible via standard AFC
_MEDIA_DIR = "/var/mobile/Media/PhoneTransfer"

# iCalendar product identifier
_PRODID = "-//PhoneTransfer//EN"

# Calendar.sqlitedb constants (see G:/test/modify_calendar.py).
_CAL_DOMAIN = "HomeDomain"
_CAL_RELPATH = "Library/Calendar/Calendar.sqlitedb"
_APPLE_EPOCH_OFFSET = 978307200
_ENTITY_TYPE_EVENT = 2
_STATUS_CONFIRMED = 1
_DEFAULT_TZ = "America/New_York"


# ---------------------------------------------------------------------------
# iCalendar serialisation
# ---------------------------------------------------------------------------

def _escape_ical_text(value: str) -> str:
    """
    Escape text values for iCalendar TEXT properties (RFC 5545 §3.3.11).

    Backslashes, semicolons, commas, and newlines require escaping.
    """
    value = value.replace("\\", "\\\\")
    value = value.replace(";", "\\;")
    value = value.replace(",", "\\,")
    value = value.replace("\n", "\\n")
    value = value.replace("\r", "")
    return value


def _fold_line(line: str) -> str:
    """
    Apply iCalendar line folding: lines longer than 75 octets (in UTF-8) are
    split with CRLF followed by a single whitespace character (RFC 5545 §3.1).
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line

    parts: list[str] = []
    while True:
        # Take up to 75 UTF-8 bytes without splitting a multibyte sequence
        chunk = encoded[:75]
        # Trim back to a valid UTF-8 boundary
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError:
            # Walk back until we find a valid boundary
            while chunk:
                try:
                    chunk.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    chunk = chunk[:-1]

        parts.append(chunk.decode("utf-8"))
        encoded = encoded[len(chunk):]
        if not encoded:
            break
        # Continuation lines start with a space; reserve 1 byte for it
        encoded = b" " + encoded

    return "\r\n".join(parts)


def _format_dt(dt: datetime) -> str:
    """
    Format a datetime as an iCalendar DATE-TIME value in UTC (suffix 'Z').
    If the datetime is naive we treat it as UTC.
    """
    if dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _event_to_vevent(event: CalendarEvent) -> list[str]:
    """
    Serialise a CalendarEvent to a list of iCalendar content lines
    (CRLF not yet applied; folding applied per line).
    """
    lines: list[str] = ["BEGIN:VEVENT"]

    uid = event.uid or str(uuid.uuid4())
    lines.append(_fold_line(f"UID:{uid}"))

    # DTSTAMP — time the record was created (required by RFC 5545)
    lines.append(f"DTSTAMP:{_format_dt(datetime.now(timezone.utc))}")

    lines.append(_fold_line(f"SUMMARY:{_escape_ical_text(event.title)}"))

    if event.all_day:
        lines.append(f"DTSTART;VALUE=DATE:{event.start.strftime('%Y%m%d')}")
        lines.append(f"DTEND;VALUE=DATE:{event.end.strftime('%Y%m%d')}")
    else:
        lines.append(f"DTSTART:{_format_dt(event.start)}")
        lines.append(f"DTEND:{_format_dt(event.end)}")

    if event.location:
        lines.append(_fold_line(f"LOCATION:{_escape_ical_text(event.location)}"))

    if event.notes:
        lines.append(_fold_line(f"DESCRIPTION:{_escape_ical_text(event.notes)}"))

    if event.recurrence_rule:
        # RRULE values are already in RFC 5545 format (e.g. FREQ=WEEKLY;…)
        lines.append(_fold_line(f"RRULE:{event.recurrence_rule}"))

    lines.append("END:VEVENT")
    return lines


def _build_ics(events: list[CalendarEvent]) -> tuple[str, int]:
    """
    Wrap all serialised VEVENT blocks in a VCALENDAR component.

    Returns (ics_text, count_of_events_included).
    Events that fail serialisation are skipped with a logged warning.
    """
    vevent_lines: list[str] = []
    count = 0
    for i, event in enumerate(events):
        try:
            vevent_lines.extend(_event_to_vevent(event))
            count += 1
        except Exception as exc:
            logger.warning(
                "inject_calendar_ios: failed to serialise event %d (%r): %s",
                i,
                event.title,
                exc,
            )

    header = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    footer = ["END:VCALENDAR"]
    all_lines = header + vevent_lines + footer
    return "\r\n".join(all_lines) + "\r\n", count


# ---------------------------------------------------------------------------
# Public inject function
# ---------------------------------------------------------------------------

def inject(
    udid: str,
    items: list[CalendarEvent],
    staging_dir: Path,
    is_jailbroken: bool = False,
) -> int:
    """
    Inject calendar events into the iOS device identified by *udid*.

    Parameters
    ----------
    udid:           iOS device UDID.
    items:          Calendar events to inject.
    staging_dir:    Local directory for temporary files.
    is_jailbroken:  Accepted for interface consistency; both paths use AFC.

    Returns
    -------
    int: Number of events successfully included in the pushed .ics file.
         Returns 0 on a total failure.
    """
    if not items:
        logger.info("inject_calendar_ios: no events to inject — done.")
        return 0

    injector = get_current_injector()
    if injector is not None:
        try:
            count = _inject_via_backup(injector, items, staging_dir)
            logger.info(
                "inject_calendar_ios: staged %d event(s) into the backup for %s",
                count, udid,
            )
            return count
        except Exception as exc:
            logger.warning(
                "inject_calendar_ios: backup-mod path failed (%s) — "
                "falling back to AFC .ics push", exc,
            )

    logger.info(
        "inject_calendar_ios: preparing %d event(s) for device %s "
        "(jailbroken=%s).",
        len(items),
        udid,
        is_jailbroken,
    )

    # ── 1. Build the .ics file in the staging area ──────────────────────────
    staging_dir.mkdir(parents=True, exist_ok=True)
    datestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_ics = staging_dir / f"calendar_{datestamp}.ics"

    try:
        ics_text, count = _build_ics(items)
        if count == 0:
            logger.error(
                "inject_calendar_ios: every event failed serialisation — aborting."
            )
            return 0

        local_ics.write_text(ics_text, encoding="utf-8")
        logger.debug(
            "inject_calendar_ios: wrote %d event(s) to staging file %s.",
            count,
            local_ics,
        )
    except Exception as exc:
        logger.error(
            "inject_calendar_ios: failed to write staging .ics: %s", exc
        )
        return 0

    # ── 2. Push the .ics to the device ─────────────────────────────────────
    broker = IOSServiceBroker(udid=udid)
    try:
        return _push_ics(broker, local_ics, count, datestamp)
    finally:
        broker.close()


# ---------------------------------------------------------------------------
# Push helper
# ---------------------------------------------------------------------------

def _push_ics(
    broker: IOSServiceBroker,
    local_ics: Path,
    count: int,
    datestamp: str,
) -> int:
    """
    Push the .ics file to /var/mobile/Media/PhoneTransfer/ via standard AFC.
    """
    logger.info(
        "inject_calendar_ios: pushing calendar.ics to device Media folder.  "
        "Open the file on the device to import events into the Calendar app."
    )
    try:
        afc = AFCConnector(broker)
    except Exception as exc:
        logger.error(
            "inject_calendar_ios: failed to open AFC service: %s", exc
        )
        return 0

    device_ics = f"{_MEDIA_DIR}/calendar_{datestamp}.ics"

    try:
        afc.makedirs(_MEDIA_DIR)
    except Exception as exc:
        logger.warning(
            "inject_calendar_ios: makedirs(%s) failed (may already exist): %s",
            _MEDIA_DIR,
            exc,
        )

    ok = afc.push_file(local_ics, device_ics)
    if ok:
        logger.info(
            "inject_calendar_ios: pushed %d event(s) to %s — "
            "open this file on the device to import into Calendar.",
            count,
            device_ics,
        )
        return count
    else:
        logger.error(
            "inject_calendar_ios: AFC push_file to %s failed.", device_ics
        )
        return 0


# ---------------------------------------------------------------------------
# Backup-mod path
# ---------------------------------------------------------------------------

def _inject_via_backup(
    injector: IOSBackupInjector,
    events: list[CalendarEvent],
    staging_dir: Path,
) -> int:
    db_path = injector.stage_db(_CAL_DOMAIN, _CAL_RELPATH)

    target_calendar_id = _resolve_target_calendar_id(db_path, staging_dir)

    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=FULL")

        creation_ts = time.time() - _APPLE_EPOCH_OFFSET
        inserted = 0
        with con:
            for ev in events:
                start_ts, start_tz, end_ts, end_tz = _event_window(ev)
                _insert_calendar_item(
                    con,
                    summary=ev.title,
                    description=ev.notes,
                    location_text=ev.location,
                    start_date=start_ts, start_tz=start_tz,
                    end_date=end_ts, end_tz=end_tz,
                    all_day=ev.all_day,
                    calendar_id=target_calendar_id,
                    creation_ts=creation_ts,
                    uid_hint=ev.uid,
                )
                inserted += 1

            # Bump sqlite_sequence + ForceRebuildSeed so CalendarAgent
            # rebuilds OccurrenceCache on the next device boot.
            max_rowid = con.execute(
                "SELECT COALESCE(MAX(ROWID), 0) FROM CalendarItem"
            ).fetchone()[0]
            if con.execute(
                "SELECT 1 FROM sqlite_sequence WHERE name='CalendarItem'"
            ).fetchone():
                con.execute(
                    "UPDATE sqlite_sequence SET seq=? WHERE name='CalendarItem'",
                    (max_rowid,),
                )
            con.execute(
                "UPDATE _SqliteDatabaseProperties SET value=value+1 "
                "WHERE key='ForceRebuildSeed'"
            )
            con.execute(
                "UPDATE _SqliteDatabaseProperties SET value=? "
                "WHERE key='ForceRebuildScheduledTaskCache'",
                ("1",),
            )
    finally:
        con.close()

    return inserted


def _resolve_target_calendar_id(db_path: Path, staging_dir: Path) -> int:
    """
    Pick which Calendar.ROWID to attach new events to.

    Priority:
      1. ``<staging>/calendar_ios_target.json`` with ``{"calendar_id": N}``.
      2. The first writable Calendar row (``read_only = 0``).
      3. Any Calendar row.
    """
    hint_path = staging_dir / "calendar_ios_target.json"
    if hint_path.is_file():
        try:
            cid = int(json.loads(hint_path.read_text(encoding="utf-8"))["calendar_id"])
            return cid
        except Exception as exc:
            logger.debug(
                "inject_calendar_ios: ignoring %s (%s)", hint_path, exc
            )

    con = sqlite3.connect(str(db_path))
    try:
        # The read-only marker has different names across iOS versions:
        # ``read_only`` on iOS 12-, ``flags`` (bit 0) on iOS 13+, sometimes
        # absent entirely.  Probe and pick whichever filter the schema
        # actually supports.
        cols = {
            r[1] for r in con.execute("PRAGMA table_info(Calendar)").fetchall()
        }
        if "read_only" in cols:
            filter_sql = "WHERE COALESCE(read_only, 0) = 0"
        elif "flags" in cols:
            # bit 0 of Calendar.flags is the immutable bit on recent iOS.
            filter_sql = "WHERE (COALESCE(flags, 0) & 1) = 0"
        else:
            filter_sql = ""
        for sql in (
            f"SELECT ROWID FROM Calendar {filter_sql} ORDER BY ROWID LIMIT 1",
            "SELECT ROWID FROM Calendar ORDER BY ROWID LIMIT 1",
        ):
            row = con.execute(sql).fetchone()
            if row:
                return row[0]
        raise RuntimeError("no Calendar rows found in Calendar.sqlitedb")
    finally:
        con.close()


def _event_window(
    ev: CalendarEvent,
) -> tuple[float, str, float, str]:
    """Return (start_apple, start_tz, end_apple, end_tz)."""
    start = ev.start
    end = ev.end
    if ev.all_day:
        # Floor to UTC midnight, end at 23:59:59 same day.
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        midnight = start.astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_same_day = midnight + _dt.timedelta(seconds=86399)
        return (
            midnight.timestamp() - _APPLE_EPOCH_OFFSET,
            "_float",
            end_same_day.timestamp() - _APPLE_EPOCH_OFFSET,
            "_float",
        )
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return (
        start.timestamp() - _APPLE_EPOCH_OFFSET, _DEFAULT_TZ,
        end.timestamp() - _APPLE_EPOCH_OFFSET, _DEFAULT_TZ,
    )


def _insert_calendar_item(
    con: sqlite3.Connection,
    *,
    summary: str,
    description: str | None,
    location_text: str | None,
    start_date: float,
    start_tz: str,
    end_date: float,
    end_tz: str,
    all_day: bool,
    calendar_id: int,
    creation_ts: float,
    uid_hint: str | None,
) -> int:
    location_id = 0
    if location_text:
        cur = con.execute(
            "INSERT INTO Location (title, address, item_owner_id) VALUES (?, ?, 0)",
            (location_text, location_text),
        )
        location_id = cur.lastrowid

    uuid_hint = (uid_hint or str(uuid.uuid4())).upper()
    unique_id = f"{uuid_hint.lower()}@phonetransfer.local"

    cur = con.execute(
        """
        INSERT INTO CalendarItem (
            summary, description,
            start_date, start_tz, end_date, end_tz,
            all_day, calendar_id,
            orig_item_id, organizer_id, self_attendee_id,
            status, invitation_status, availability, privacy_level,
            last_modified, sequence_num,
            birthday_id, modified_properties,
            external_tracking_status,
            UUID, unique_identifier,
            hidden, has_recurrences, has_attachment, has_attendees,
            entity_type, priority,
            due_all_day,
            creation_date,
            display_order,
            created_by_id, modified_by_id,
            invitation_changed_properties, default_alarm_removed,
            travel_advisory_behavior,
            start_location_id, end_location_id, suggested_event_info_id,
            can_forward, location_prediction_state, fired_ttl,
            disallow_propose_new_time, junk_status, flags,
            location_id, client_location_id
        ) VALUES (
            ?, ?,
            ?, ?, ?, ?,
            ?, ?,
            0, 0, 0,
            ?, 0, 0, 0,
            ?, 0,
            -1, 0,
            0,
            ?, ?,
            0, 0, 0, 0,
            ?, 0,
            0,
            ?,
            ?,
            -1, -1,
            0, 0,
            0,
            0, 0, 0,
            1, 0, 0,
            0, 0, 0,
            ?, 0
        )
        """,
        (
            summary, description,
            start_date, start_tz, end_date, end_tz,
            1 if all_day else 0, calendar_id,
            _STATUS_CONFIRMED,
            creation_ts,
            uuid_hint, unique_id,
            _ENTITY_TYPE_EVENT,
            creation_ts,
            int(creation_ts),
            location_id,
        ),
    )
    item_id = cur.lastrowid
    if location_id:
        con.execute(
            "UPDATE Location SET item_owner_id=? WHERE ROWID=?",
            (item_id, location_id),
        )
    return item_id
