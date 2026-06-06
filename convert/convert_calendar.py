"""
convert_calendar.py

Converts CalendarEvent objects to/from iCalendar (RFC 5545) format.
Only stdlib is used — no icalendar library dependency.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.normalization_schema import CalendarEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# iCalendar text-field escaping (RFC 5545 §3.3.11)
# ---------------------------------------------------------------------------

def _ical_escape(value: str) -> str:
    """Escape backslash, semicolon, comma, and newline per RFC 5545."""
    value = value.replace("\\", "\\\\")
    value = value.replace(";", "\\;")
    value = value.replace(",", "\\,")
    value = value.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return value


def _ical_unescape(value: str) -> str:
    """Reverse RFC 5545 text escaping."""
    value = value.replace("\\n", "\n").replace("\\N", "\n")
    value = value.replace("\\;", ";").replace("\\,", ",").replace("\\\\", "\\")
    return value


def _fold_ical(line: str) -> str:
    """
    Fold an iCalendar content line at 75 octets, with CRLF + space continuation
    (RFC 5545 §3.1).
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line + "\r\n"

    result_parts: list[str] = []
    buf = b""
    for ch in line:
        ch_b = ch.encode("utf-8")
        if len(buf) + len(ch_b) > 75:
            result_parts.append(buf.decode("utf-8"))
            buf = b" " + ch_b
        else:
            buf += ch_b
    if buf:
        result_parts.append(buf.decode("utf-8"))
    return "\r\n".join(result_parts) + "\r\n"


# ---------------------------------------------------------------------------
# Date/time formatting helpers
# ---------------------------------------------------------------------------

def _fmt_datetime(dt: datetime) -> str:
    """Format a datetime as UTC iCalendar DATETIME: YYYYMMDDTHHMMSSz."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y%m%dT%H%M%SZ")


def _fmt_date(dt: datetime) -> str:
    """Format a datetime as iCalendar DATE: YYYYMMDD."""
    return dt.strftime("%Y%m%d")


def _parse_ical_dt(raw: str) -> datetime:
    """
    Parse iCalendar DTSTART / DTEND values.

    Supports:
    - DATE format: ``YYYYMMDD``
    - DATETIME format: ``YYYYMMDDTHHMMSS``, ``YYYYMMDDTHHMMSSZ``
    - TZID prefix: ``TZID=America/New_York:YYYYMMDDTHHMMSS``  (treated as UTC)
    """
    # Strip TZID=... prefix if present
    if ":" in raw:
        raw = raw.split(":", 1)[-1]
    raw = raw.strip()

    # DATE-only
    if len(raw) == 8:
        return datetime.strptime(raw, "%Y%m%d").replace(tzinfo=timezone.utc)

    # DATETIME with Z suffix
    if raw.endswith("Z"):
        return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)

    # DATETIME without timezone → assume UTC
    return datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# CalendarEvent → iCalendar VEVENT string
# ---------------------------------------------------------------------------

def event_to_ical(event: CalendarEvent) -> str:
    """
    Build a VEVENT component string from a CalendarEvent.

    - All-day events use DATE format for DTSTART/DTEND and include no time.
    - Timed events use UTC DATETIME format.
    - Text fields (SUMMARY, DESCRIPTION, LOCATION) are escaped per RFC 5545.
    - A UID is generated if not present on the event.
    """
    lines: list[str] = []

    def prop(name: str, value: str) -> None:
        lines.append(_fold_ical(f"{name}:{value}"))

    lines.append("BEGIN:VEVENT\r\n")

    uid = event.uid or str(uuid.uuid4())
    prop("UID", uid)

    if event.all_day:
        prop("DTSTART;VALUE=DATE", _fmt_date(event.start))
        prop("DTEND;VALUE=DATE", _fmt_date(event.end))
    else:
        prop("DTSTART", _fmt_datetime(event.start))
        prop("DTEND", _fmt_datetime(event.end))

    if event.title:
        prop("SUMMARY", _ical_escape(event.title))

    if event.notes:
        prop("DESCRIPTION", _ical_escape(event.notes))

    if event.location:
        prop("LOCATION", _ical_escape(event.location))

    if event.recurrence_rule:
        # Strip leading RRULE: prefix if caller already included it
        rrule = event.recurrence_rule
        if not rrule.upper().startswith("RRULE:"):
            rrule = "RRULE:" + rrule
        lines.append(_fold_ical(rrule))

    lines.append("END:VEVENT\r\n")
    return "".join(lines)


# ---------------------------------------------------------------------------
# Multiple events → iCalendar file
# ---------------------------------------------------------------------------

def events_to_ical_file(events: list[CalendarEvent], path: Path) -> Path:
    """
    Wrap events in a VCALENDAR envelope and write to *path* (UTF-8, CRLF).

    Returns *path*.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    parts = [
        "BEGIN:VCALENDAR\r\n",
        "VERSION:2.0\r\n",
        "PRODID:-//PhoneTransfer//EN\r\n",
    ]
    for event in events:
        parts.append(event_to_ical(event))
    parts.append("END:VCALENDAR\r\n")

    path.write_text("".join(parts), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# iCalendar string → list[CalendarEvent]
# ---------------------------------------------------------------------------

def _unfold_ical(text: str) -> str:
    """Unfold iCalendar lines (CRLF or LF followed by space or tab)."""
    text = re.sub(r"\r\n[ \t]", "", text)
    text = re.sub(r"\n[ \t]", "", text)
    return text


def ical_to_events(ical_str: str) -> list[CalendarEvent]:
    """
    Parse an iCalendar string and return a list of CalendarEvent objects.

    Uses a simple line-by-line parser; does not depend on any third-party
    library.  Handles DATE and DATETIME formats; ignores TZID (treats as UTC).
    """
    unfolded = _unfold_ical(ical_str)
    events: list[CalendarEvent] = []

    # Split on VEVENT boundaries
    vevent_pattern = re.compile(
        r"BEGIN:VEVENT\r?\n(.*?)END:VEVENT",
        re.IGNORECASE | re.DOTALL,
    )

    for match in vevent_pattern.finditer(unfolded):
        block = match.group(1)
        props: dict[str, str] = {}

        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            # Property name may include parameters: NAME;PARAM=VAL:value
            colon = line.find(":")
            if colon == -1:
                continue
            prop_and_params = line[:colon]
            value = line[colon + 1:]
            # Base property name (strip parameters)
            prop_name = prop_and_params.split(";")[0].upper()
            # For DTSTART/DTEND preserve full left-side so _parse_ical_dt can handle TZID
            if prop_name in ("DTSTART", "DTEND"):
                # Store the parameters+value together for datetime parsing
                props[prop_name + "_RAW"] = prop_and_params.split(";", 1)[-1] + ":" + value if ";" in prop_and_params else value
            else:
                props.setdefault(prop_name, value)

        title = _ical_unescape(props.get("SUMMARY", ""))
        notes = _ical_unescape(props.get("DESCRIPTION", "")) or None
        location = _ical_unescape(props.get("LOCATION", "")) or None
        uid = props.get("UID") or None
        rrule = props.get("RRULE") or None

        dtstart_raw = props.get("DTSTART_RAW", props.get("DTSTART", ""))
        dtend_raw = props.get("DTEND_RAW", props.get("DTEND", ""))

        all_day = False
        try:
            start = _parse_ical_dt(dtstart_raw)
            # DATE-only entries (no T in value after stripping TZID prefix)
            clean = dtstart_raw.split(":", 1)[-1].strip()
            if "T" not in clean:
                all_day = True
        except Exception as exc:
            logger.warning("Unparseable DTSTART %r, skipping event: %s", dtstart_raw, exc)
            continue

        try:
            end = _parse_ical_dt(dtend_raw)
        except Exception:
            end = start

        events.append(CalendarEvent(
            title=title,
            start=start,
            end=end,
            all_day=all_day,
            uid=uid,
            location=location,
            notes=notes,
            recurrence_rule=rrule,
        ))

    return events


def ical_file_to_events(path: Path) -> list[CalendarEvent]:
    """
    Read an iCalendar file from *path* and return a list of CalendarEvent objects.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return ical_to_events(text)
