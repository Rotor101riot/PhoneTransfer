from __future__ import annotations

import logging
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.config_loader import get_config
from core.normalization_schema import Reminder

logger = logging.getLogger(__name__)

_CALENDAR_URI = "content://com.android.calendar/events"
_SDCARD_ICS = "/sdcard/PhoneTransfer/reminders_import.ics"


def _format_dt(dt: datetime) -> str:
    """Format a datetime as iCalendar YYYYMMDDTHHMMSSZ."""
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _escape_ics(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
        .replace("\r", "")
    )


def _build_ics(items: list[Reminder]) -> str:
    """Build a VCALENDAR string with VTODO components."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//PhoneTransfer//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for reminder in items:
        uid = reminder.uid or str(uuid.uuid4())
        title = _escape_ics(reminder.title or "Reminder")
        notes = _escape_ics(reminder.notes or "") if reminder.notes else ""
        status = "COMPLETED" if reminder.completed else "NEEDS-ACTION"
        priority = max(0, min(9, int(reminder.priority or 0)))

        lines += [
            "BEGIN:VTODO",
            f"UID:{uid}",
            f"SUMMARY:{title}",
        ]
        if reminder.due:
            lines.append(f"DUE:{_format_dt(reminder.due)}")
        if notes:
            lines.append(f"DESCRIPTION:{notes}")
        lines += [
            f"STATUS:{status}",
            f"PRIORITY:{priority}",
            "END:VTODO",
        ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def _insert_reminder_as_event(
    adb: str, device_id: str, reminder: Reminder
) -> bool:
    """Insert a Reminder as a CalendarContract event via content provider."""
    due_ms = (
        int(reminder.due.timestamp() * 1000)
        if reminder.due
        else _now_ms()
    )
    dtend_ms = due_ms + 3_600_000  # +1 hour
    title = (reminder.title or "Reminder").replace("'", "\\'")
    notes = (reminder.notes or "").replace("'", "\\'")

    cmd = [
        adb, "-s", device_id, "shell",
        "content", "insert",
        "--uri", _CALENDAR_URI,
        "--bind", f"title:s:{title}",
        "--bind", f"description:s:{notes}",
        "--bind", f"dtstart:l:{due_ms}",
        "--bind", f"dtend:l:{dtend_ms}",
        "--bind", "allDay:i:0",
        "--bind", "calendar_id:i:1",
        "--bind", "eventTimezone:s:UTC",
        "--bind", "status:i:0",
    ]
    try:
        result = _run(cmd)
        if result.returncode == 0:
            return True
        logger.debug(
            "CalendarContract insert failed (rc=%d): %s",
            result.returncode,
            result.stderr.strip(),
        )
        return False
    except Exception as exc:
        logger.warning("Exception during CalendarContract insert: %s", exc)
        return False


def _push_ics(
    adb: str, device_id: str, local_ics: Path, staging_dir: Path
) -> None:
    """Push the ICS file to /sdcard/PhoneTransfer/ on the device."""
    try:
        _run([adb, "-s", device_id, "shell", "mkdir", "-p", "/sdcard/PhoneTransfer"])
        result = _run([adb, "-s", device_id, "push", str(local_ics), _SDCARD_ICS])
        if result.returncode == 0:
            logger.info(
                "ICS file pushed to %s on device %s. "
                "It is available for manual import.",
                _SDCARD_ICS,
                device_id,
            )
        else:
            logger.warning(
                "Failed to push ICS to device %s: %s",
                device_id,
                result.stderr.strip(),
            )
    except Exception as exc:
        logger.warning("Exception pushing ICS to device %s: %s", device_id, exc)


def inject(
    device_id: str, items: list[Reminder], staging_dir: Path, is_privileged: bool
) -> int:
    """Inject reminders into Android.

    Inserts each reminder as a CalendarContract event and also pushes a
    VTODO .ics file to /sdcard/PhoneTransfer/reminders_import.ics for
    alternative manual import.

    Args:
        device_id: ADB serial number.
        items: Reminder objects to inject.
        staging_dir: Local directory for temporary files.
        is_privileged: True if root access is available (not required here).

    Returns:
        Number of reminders successfully inserted as calendar events.
    """
    if not items:
        logger.info("No reminders to inject for device %s.", device_id)
        return 0

    staging_dir.mkdir(parents=True, exist_ok=True)
    cfg = get_config()
    adb = cfg.adb_exe

    # Generate and push the ICS file regardless of content provider success
    local_ics = staging_dir / "reminders_import.ics"
    try:
        local_ics.write_text(_build_ics(items), encoding="utf-8")
        _push_ics(adb, device_id, local_ics, staging_dir)
    except Exception as exc:
        logger.warning("Could not generate/push ICS file: %s", exc)

    # Insert via CalendarContract
    inserted = 0
    for reminder in items:
        if _insert_reminder_as_event(adb, device_id, reminder):
            inserted += 1
        else:
            logger.warning(
                "Failed to insert reminder '%s' for device %s.",
                reminder.title,
                device_id,
            )

    logger.info(
        "Inserted %d / %d reminder(s) as calendar events on device %s.",
        inserted,
        len(items),
        device_id,
    )
    return inserted
