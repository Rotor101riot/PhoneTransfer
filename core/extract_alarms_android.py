from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from core.config_loader import get_config
from core.normalization_schema import Alarm

logger = logging.getLogger(__name__)

# AOSP / Google Clock content provider URIs to try in order
_ALARM_URIS = [
    "content://com.android.deskclock/alarm",
    "content://com.google.android.deskclock/alarm",
]

# SQLite DB paths tried when content provider is unavailable (root required)
_SQLITE_PATHS = [
    "/data/data/com.google.android.deskclock/databases/alarms.db",
    "/data/data/com.android.deskclock/databases/alarms.db",
]
_SQLITE_QUERY = (
    "SELECT hour,minutes,daysofweek,enabled,label,ringtone FROM alarm_templates"
)


def _bitmask_to_days(bitmask: int) -> list[int]:
    """Convert AOSP daysofweek bitmask to ISO repeat_days list.

    AOSP: bit 0 = Mon (1), bit 1 = Tue (2), ..., bit 6 = Sun (64).
    ISO:  0 = Mon, 1 = Tue, ..., 6 = Sun.
    """
    days: list[int] = []
    for iso_day in range(7):
        if bitmask & (1 << iso_day):
            days.append(iso_day)
    return days


def _parse_content_provider_output(output: str) -> list[Alarm]:
    """Parse the columnar output of `adb shell content query`."""
    alarms: list[Alarm] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("Row:"):
            continue
        # Example row:
        # Row: 0 _id=1, hour=7, minutes=30, daysofweek=31, enabled=1, label=Wake, ringtone=...
        try:
            # Strip leading "Row: N " prefix
            _, _, rest = line.partition(" ")  # drop "Row:"
            _, _, rest = rest.partition(" ")  # drop row index
            fields: dict[str, str] = {}
            for part in rest.split(", "):
                if "=" in part:
                    k, _, v = part.partition("=")
                    fields[k.strip()] = v.strip()
            hour = int(fields.get("hour", 0))
            minute = int(fields.get("minutes", 0))
            enabled = fields.get("enabled", "1") not in ("0", "false", "False")
            label = fields.get("label", "")
            ringtone = fields.get("ringtone") or None
            bitmask = int(fields.get("daysofweek", 0))
            repeat_days = _bitmask_to_days(bitmask)
            alarms.append(
                Alarm(
                    hour=hour,
                    minute=minute,
                    label=label,
                    enabled=enabled,
                    repeat_days=repeat_days,
                    sound=ringtone,
                )
            )
        except Exception as exc:
            logger.warning("Could not parse alarm row %r: %s", line, exc)
    return alarms


def _parse_sqlite_output(output: str) -> list[Alarm]:
    """Parse pipe-delimited sqlite3 output.

    Expected columns: hour|minutes|daysofweek|enabled|label|ringtone
    """
    alarms: list[Alarm] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        try:
            hour = int(parts[0])
            minute = int(parts[1])
            bitmask = int(parts[2])
            enabled = parts[3] not in ("0", "false", "False")
            label = parts[4] if len(parts) > 4 else ""
            ringtone = parts[5] if len(parts) > 5 else None
            repeat_days = _bitmask_to_days(bitmask)
            alarms.append(
                Alarm(
                    hour=hour,
                    minute=minute,
                    label=label,
                    enabled=enabled,
                    repeat_days=repeat_days,
                    sound=ringtone or None,
                )
            )
        except Exception as exc:
            logger.warning("Could not parse sqlite alarm row %r: %s", line, exc)
    return alarms


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
    )


def extract(device_id: str, staging_dir: Path, is_privileged: bool) -> list[Alarm]:
    """Extract alarms from an Android device.

    Tries content providers first (AOSP then Google Clock), then falls back to
    direct SQLite access if the device is rooted.

    Args:
        device_id: ADB serial number.
        staging_dir: Local directory for temporary files.
        is_privileged: True if root access is available.

    Returns:
        A list of Alarm objects, or [] on failure.
    """
    cfg = get_config()
    adb = cfg.adb_exe

    # --- Try content providers ---
    for uri in _ALARM_URIS:
        try:
            result = _run(
                [adb, "-s", device_id, "shell", "content", "query", "--uri", uri]
            )
            if result.returncode == 0 and "Row:" in result.stdout:
                alarms = _parse_content_provider_output(result.stdout)
                logger.info(
                    "Extracted %d alarms from content provider %s on device %s",
                    len(alarms),
                    uri,
                    device_id,
                )
                return alarms
            else:
                logger.debug(
                    "Content provider %s returned no rows or error for device %s: %s",
                    uri,
                    device_id,
                    result.stderr.strip(),
                )
        except Exception as exc:
            logger.warning(
                "Content provider query %s failed for device %s: %s",
                uri,
                device_id,
                exc,
            )

    # --- Fall back to sqlite3 via root ---
    if not is_privileged:
        logger.warning(
            "No content provider returned alarms and device %s is not rooted; "
            "cannot extract alarms via SQLite.",
            device_id,
        )
        return []

    for db_path in _SQLITE_PATHS:
        try:
            cmd = [
                adb,
                "-s",
                device_id,
                "shell",
                "su",
                "-c",
                f'sqlite3 {db_path} "{_SQLITE_QUERY}"',
            ]
            result = _run(cmd)
            if result.returncode == 0 and result.stdout.strip():
                alarms = _parse_sqlite_output(result.stdout)
                logger.info(
                    "Extracted %d alarms from sqlite3 %s on device %s",
                    len(alarms),
                    db_path,
                    device_id,
                )
                return alarms
            else:
                logger.debug(
                    "sqlite3 query on %s returned no data for device %s: %s",
                    db_path,
                    device_id,
                    result.stderr.strip(),
                )
        except Exception as exc:
            logger.warning(
                "sqlite3 fallback on %s failed for device %s: %s",
                db_path,
                device_id,
                exc,
            )

    logger.error("All alarm extraction methods failed for device %s.", device_id)
    return []
