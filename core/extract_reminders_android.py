"""
extract_reminders_android.py
Extract tasks / reminders from an Android device via ADB and the
CalendarProvider content provider.

Background:
  Android does not ship a dedicated Reminders app.  Tasks are stored in the
  CalendarProvider as calendar events with eventType=2 (TYPE_TASKS / VTODO).
  Google Tasks, Samsung Reminders, and third-party apps write to this same
  provider, so this is the most portable extraction path available without
  root access.

  If no tasks are found — which is common on devices where no task app has
  written to the CalendarProvider — the function returns an empty list rather
  than raising an error.

Strategy:
  1. Query content://com.android.calendar/events with a WHERE clause of
     "eventType=2" to retrieve only task/VTODO entries.
  2. Parse the returned rows into Reminder objects.
  3. Log a warning (not an error) if the query fails, because many OEM ROMs
     restrict CalendarProvider access to system apps.

Requires:
  - ADB available at the path returned by core.config_loader.get_config().
  - USB debugging enabled on the target device.
"""

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from core.config_loader import get_config
from core.normalization_schema import Reminder

logger = logging.getLogger(__name__)

EVENTS_URI = "content://com.android.calendar/events"
PROJECTION = "title,dtstart,allDay,description,hasAlarm,status"


def _adb(device_id: str, *args: str, adb_path: str = "adb") -> subprocess.CompletedProcess:
    cmd = [adb_path, "-s", device_id, *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def _parse_content_rows(output: str) -> list[dict[str, str]]:
    """
    Parse the tabular output of 'adb shell content query'.

    Each result row looks like:
        Row: 0 title=Buy milk, dtstart=1700000000000, allDay=0, ...
    """
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("Row:"):
            continue
        try:
            payload = line.split(" ", 2)[2]
        except IndexError:
            continue

        record: dict[str, str] = {}
        for token in payload.split(", "):
            if "=" in token:
                key, _, value = token.partition("=")
                record[key.strip()] = value.strip()
        if record:
            rows.append(record)
    return rows


def extract(device_id: str, staging_dir: Path, is_privileged: bool) -> list[Reminder]:
    """
    Extract reminders / tasks from an Android device.

    Parameters
    ----------
    device_id:
        The ADB serial of the target device.
    staging_dir:
        Local staging directory (not used here, kept for API consistency).
    is_privileged:
        True if the device is rooted (reserved for future use).

    Returns
    -------
    list[Reminder]
        One entry per task/VTODO row found in the CalendarProvider.
        Returns an empty list — without raising — if the provider is
        inaccessible or contains no task entries.
    """
    config = get_config()
    adb_path: str = str(config.adb_exe)

    result = _adb(
        device_id,
        "shell",
        "content",
        "query",
        "--uri",
        EVENTS_URI,
        "--projection",
        PROJECTION,
        "--where",
        "eventType=2",
        adb_path=adb_path,
    )

    if result.returncode != 0:
        logger.warning(
            "extract_reminders_android: CalendarProvider query failed (rc=%d): %s. "
            "This is common on devices where the provider restricts third-party access. "
            "Returning empty list.",
            result.returncode,
            result.stderr.strip(),
        )
        return []

    output = result.stdout.strip()

    # Some ROMs print "No result found." instead of returning a non-zero exit
    # code when the WHERE clause matches nothing or access is denied.
    if not output or "No result found" in output:
        logger.info(
            "extract_reminders_android: no task entries found in CalendarProvider "
            "(eventType=2). The device may not have a tasks app installed, or tasks "
            "are stored in a proprietary database not accessible via ADB."
        )
        return []

    rows = _parse_content_rows(output)
    if not rows:
        logger.info("extract_reminders_android: CalendarProvider returned no parseable rows")
        return []

    logger.info("extract_reminders_android: found %d task row(s)", len(rows))

    reminders: list[Reminder] = []
    for row in rows:
        title = row.get("title", "").strip() or "(untitled)"
        description = row.get("description", "").strip() or None
        dtstart_str = row.get("dtstart", "").strip()
        has_alarm = row.get("hasAlarm", "0").strip() == "1"

        # dtstart is milliseconds since epoch in Android's CalendarProvider.
        due: datetime | None = None
        if dtstart_str.lstrip("-").isdigit():
            try:
                due = datetime.fromtimestamp(int(dtstart_str) / 1000.0, tz=timezone.utc)
            except (OSError, OverflowError, ValueError) as exc:
                logger.debug(
                    "extract_reminders_android: cannot parse dtstart=%s: %s",
                    dtstart_str,
                    exc,
                )

        reminder = Reminder(
            title=title,
            notes=description,
            due=due,
            has_alarm=has_alarm,
        )
        reminders.append(reminder)
        logger.debug("extract_reminders_android: parsed task (title omitted)")

    logger.info(
        "extract_reminders_android: extracted %d reminder(s) from device %s",
        len(reminders),
        device_id,
    )
    return reminders
