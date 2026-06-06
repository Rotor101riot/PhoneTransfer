"""
inject_calendar_android.py

Injects CalendarEvent records into an Android device connected via ADB.

Strategy
--------
Non-rooted path (default):
    Build a single RFC 5545 .ics file containing all events, push it to
    /sdcard/PhoneTransfer/calendar_import.ics via `adb push`, and log clear
    instructions for the user to import it through the device's Calendar app.
    The count of events successfully serialised to the .ics file is returned.

Rooted path:
    Use `adb shell content insert --uri content://com.android.calendar/events`
    to insert each event directly into the CalendarContract provider.
    calendar_id 1 (the default local calendar) is assumed.  Events that fail
    to insert are counted but not fatal — the function continues and returns
    the success count.

iCalendar implementation follows RFC 5545 (line folding, text escaping).
Datetime values are always written in UTC (suffix 'Z').
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.normalization_schema import CalendarEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REMOTE_DIR         = "/sdcard/PhoneTransfer"
_REMOTE_ICS         = f"{_REMOTE_DIR}/calendar_import.ics"
_DEFAULT_CALENDAR_ID = 1          # "Local" calendar on most Android devices
_PRODID             = "-//PhoneTransfer//EN"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def inject(
    device_id: str,
    items: list[CalendarEvent],
    staging_dir: Path,
    is_rooted: bool,
) -> int:
    """
    Inject calendar events into the Android device identified by *device_id*.

    Parameters
    ----------
    device_id:   ADB device serial string.
    items:       CalendarEvent objects to inject.
    staging_dir: Local directory for temporary files.
    is_rooted:   If True, use direct content provider insertion.

    Returns
    -------
    int: Count of events successfully injected (or written to .ics).
         Returns 0 on total failure.
    """
    try:
        return _inject_impl(device_id, items, staging_dir, is_rooted)
    except Exception as exc:
        logger.exception(
            "[calendar_inject/android] Top-level failure for %s: %s",
            device_id,
            exc,
        )
        return 0


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------

def _inject_impl(
    device_id: str,
    items: list[CalendarEvent],
    staging_dir: Path,
    is_rooted: bool,
) -> int:
    from core.adb_manager import ADBManager
    from core.config_loader import get_config

    if not items:
        logger.info(
            "[calendar_inject/android] No events to inject — done."
        )
        return 0

    logger.info(
        "[calendar_inject/android] Injecting %d event(s) to %s (rooted=%s)",
        len(items),
        device_id,
        is_rooted,
    )

    adb = ADBManager(get_config())

    if is_rooted:
        return _inject_rooted(device_id, items, adb)
    else:
        return _inject_via_ics(device_id, items, staging_dir, adb)


# ---------------------------------------------------------------------------
# Non-rooted path — push .ics file
# ---------------------------------------------------------------------------

def _inject_via_ics(
    device_id: str,
    items: list[CalendarEvent],
    staging_dir: Path,
    adb,
) -> int:
    """
    Serialise all events to a single .ics file, push to /sdcard/PhoneTransfer/,
    and log import instructions for the user.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    local_ics = staging_dir / "calendar_android_inject.ics"

    ics_text, count = _build_ics(items)
    if count == 0:
        logger.error(
            "[calendar_inject/android] Every event failed serialisation — aborting."
        )
        return 0

    try:
        local_ics.write_text(ics_text, encoding="utf-8")
        logger.debug(
            "[calendar_inject/android] Wrote %d event(s) to staging: %s",
            count,
            local_ics,
        )
    except Exception as exc:
        logger.error(
            "[calendar_inject/android] Failed to write local .ics: %s", exc
        )
        return 0

    # Ensure remote directory exists
    adb.shell(device_id, f"mkdir -p {_REMOTE_DIR}", timeout=10)

    ok = adb.push(device_id, local_ics, _REMOTE_ICS, timeout=60)
    if not ok:
        logger.error(
            "[calendar_inject/android] adb push of calendar .ics failed."
        )
        return 0

    logger.info(
        "[calendar_inject/android] Pushed %d event(s) to %s.  "
        "To import: open the device's Calendar app → Settings → Import, "
        "or use a file manager to open %s on the device.",
        count,
        _REMOTE_ICS,
        _REMOTE_ICS,
    )
    return count


# ---------------------------------------------------------------------------
# Rooted path — direct content provider insertion
# ---------------------------------------------------------------------------

def _inject_rooted(
    device_id: str,
    items: list[CalendarEvent],
    adb,
) -> int:
    """
    Insert events directly via `adb shell content insert` using the
    CalendarContract events URI.  Requires a rooted device.
    """
    uri = "content://com.android.calendar/events"
    success = 0

    for i, event in enumerate(items):
        try:
            cmd = _build_insert_cmd(uri, event)
        except Exception as exc:
            logger.warning(
                "[calendar_inject/android] Failed to build insert cmd for "
                "event %d (%r): %s",
                i,
                event.title,
                exc,
            )
            continue

        stdout, stderr, rc = adb.shell(device_id, cmd, timeout=15)
        if rc == 0:
            success += 1
            logger.debug(
                "[calendar_inject/android] Inserted event %d: %r",
                i,
                event.title,
            )
        else:
            logger.warning(
                "[calendar_inject/android] content insert failed for "
                "event %d (%r) rc=%d: %s",
                i,
                event.title,
                rc,
                stderr.strip(),
            )

    logger.info(
        "[calendar_inject/android] Rooted insert: %d/%d event(s) succeeded.",
        success,
        len(items),
    )
    return success


def _build_insert_cmd(uri: str, event: CalendarEvent) -> str:
    """
    Build the `content insert` shell command string for a single CalendarEvent.
    Datetime values are converted to UTC milliseconds since epoch.
    """
    dtstart_ms = _dt_to_ms(event.start)
    dtend_ms   = _dt_to_ms(event.end)
    all_day_i  = 1 if event.all_day else 0
    title      = _shell_escape(event.title)

    parts: list[str] = [
        f"content insert --uri {uri}",
        f"--bind title:s:{title}",
        f"--bind dtstart:l:{dtstart_ms}",
        f"--bind dtend:l:{dtend_ms}",
        f"--bind allDay:i:{all_day_i}",
        f"--bind calendar_id:i:{_DEFAULT_CALENDAR_ID}",
        "--bind eventTimezone:s:UTC",
    ]

    if event.location:
        parts.append(f"--bind eventLocation:s:{_shell_escape(event.location)}")

    if event.notes:
        parts.append(f"--bind description:s:{_shell_escape(event.notes)}")

    if event.recurrence_rule:
        parts.append(f"--bind rrule:s:{_shell_escape(event.recurrence_rule)}")

    return " ".join(parts)


def _dt_to_ms(dt: datetime) -> int:
    """Convert a timezone-aware datetime to Unix milliseconds (UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp() * 1000)


def _shell_escape(value: str) -> str:
    """
    Wrap a string in single quotes and escape any embedded single quotes
    for use in an adb shell command.
    """
    escaped = value.replace("'", "'\\''")
    return f"'{escaped}'"


# ---------------------------------------------------------------------------
# iCalendar serialisation (shared with non-rooted path)
# ---------------------------------------------------------------------------

def _escape_ical_text(value: str) -> str:
    """Escape TEXT property values per RFC 5545 §3.3.11."""
    value = value.replace("\\", "\\\\")
    value = value.replace(";", "\\;")
    value = value.replace(",", "\\,")
    value = value.replace("\n", "\\n")
    value = value.replace("\r", "")
    return value


def _fold_line(line: str) -> str:
    """
    Apply RFC 5545 §3.1 line folding: lines exceeding 75 octets (UTF-8) are
    split with CRLF + single whitespace, without splitting multibyte sequences.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line

    parts: list[str] = []
    while encoded:
        chunk = encoded[:75]
        # Walk back to a valid UTF-8 boundary if needed
        while chunk:
            try:
                chunk.decode("utf-8")
                break
            except UnicodeDecodeError:
                chunk = chunk[:-1]
        parts.append(chunk.decode("utf-8"))
        encoded = encoded[len(chunk):]
        if encoded:
            encoded = b" " + encoded  # continuation lines start with a space

    return "\r\n".join(parts)


def _format_dt_utc(dt: datetime) -> str:
    """Format datetime as iCalendar UTC DATE-TIME (suffix 'Z')."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _event_to_vevent(event: CalendarEvent) -> list[str]:
    """Serialise a CalendarEvent to a list of iCalendar content lines."""
    lines: list[str] = ["BEGIN:VEVENT"]

    event_uid = event.uid or str(uuid.uuid4())
    lines.append(_fold_line(f"UID:{event_uid}"))
    lines.append(f"DTSTAMP:{_format_dt_utc(datetime.now(timezone.utc))}")
    lines.append(_fold_line(f"SUMMARY:{_escape_ical_text(event.title)}"))

    if event.all_day:
        lines.append(f"DTSTART;VALUE=DATE:{event.start.strftime('%Y%m%d')}")
        lines.append(f"DTEND;VALUE=DATE:{event.end.strftime('%Y%m%d')}")
    else:
        lines.append(f"DTSTART:{_format_dt_utc(event.start)}")
        lines.append(f"DTEND:{_format_dt_utc(event.end)}")

    if event.location:
        lines.append(
            _fold_line(f"LOCATION:{_escape_ical_text(event.location)}")
        )

    if event.notes:
        lines.append(
            _fold_line(f"DESCRIPTION:{_escape_ical_text(event.notes)}")
        )

    if event.recurrence_rule:
        lines.append(_fold_line(f"RRULE:{event.recurrence_rule}"))

    lines.append("END:VEVENT")
    return lines


def _build_ics(events: list[CalendarEvent]) -> tuple[str, int]:
    """
    Wrap all serialised VEVENT blocks in a VCALENDAR component.
    Returns (ics_text, count_of_events_included).
    """
    vevent_lines: list[str] = []
    count = 0
    for i, event in enumerate(events):
        try:
            vevent_lines.extend(_event_to_vevent(event))
            count += 1
        except Exception as exc:
            logger.warning(
                "[calendar_inject/android] Skipping event %d (%r): %s",
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
