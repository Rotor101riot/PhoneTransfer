from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from core.config_loader import get_config
from core.normalization_schema import Alarm

logger = logging.getLogger(__name__)

_ALARM_URIS = [
    "content://com.android.deskclock/alarm",
    "content://com.google.android.deskclock/alarm",
]


def _days_to_bitmask(repeat_days: list[int]) -> int:
    """Convert ISO repeat_days (0=Mon..6=Sun) to AOSP daysofweek bitmask.

    AOSP: bit 0 = Mon (value 1), bit 1 = Tue (value 2), ..., bit 6 = Sun (value 64).
    """
    mask = 0
    for iso_day in repeat_days:
        mask |= 1 << iso_day
    return mask


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def _insert_alarm(adb: str, device_id: str, uri: str, alarm: Alarm) -> bool:
    """Attempt to insert a single alarm via a content provider URI.

    Returns True if successful.
    """
    bitmask = _days_to_bitmask(alarm.repeat_days)
    enabled_int = 1 if alarm.enabled else 0
    label = alarm.label or ""

    cmd = [
        adb,
        "-s",
        device_id,
        "shell",
        "content",
        "insert",
        "--uri",
        uri,
        "--bind",
        f"hour:i:{alarm.hour}",
        "--bind",
        f"minutes:i:{alarm.minute}",
        "--bind",
        f"enabled:i:{enabled_int}",
        "--bind",
        f"label:s:{label}",
        "--bind",
        f"daysofweek:i:{bitmask}",
    ]
    try:
        result = _run(cmd)
        if result.returncode == 0:
            return True
        logger.debug(
            "content insert via %s failed (rc=%d): %s",
            uri,
            result.returncode,
            result.stderr.strip(),
        )
        return False
    except Exception as exc:
        logger.warning("Exception during content insert via %s: %s", uri, exc)
        return False


def inject(
    device_id: str, items: list[Alarm], staging_dir: Path, is_privileged: bool
) -> int:
    """Inject alarms into Android via content provider.

    Tries AOSP Clock URI first, then Google Clock URI. Counts successful inserts.

    Args:
        device_id: ADB serial number.
        items: Alarm objects to inject.
        staging_dir: Local directory for temporary files (unused here).
        is_privileged: True if root access is available (not required for this path).

    Returns:
        Number of alarms successfully inserted.
    """
    if not items:
        logger.info("No alarms to inject for device %s.", device_id)
        return 0

    cfg = get_config()
    adb = cfg.adb_exe

    # Determine which URI works by probing with the first alarm
    working_uri: str | None = None
    for uri in _ALARM_URIS:
        if _insert_alarm(adb, device_id, uri, items[0]):
            working_uri = uri
            logger.info("Using alarm content provider URI: %s", uri)
            break

    if working_uri is None:
        logger.error(
            "No working alarm content provider found for device %s. "
            "Tried: %s",
            device_id,
            ", ".join(_ALARM_URIS),
        )
        return 0

    # First alarm was already inserted successfully
    inserted = 1
    for alarm in items[1:]:
        if _insert_alarm(adb, device_id, working_uri, alarm):
            inserted += 1
        else:
            # Try fallback URI if primary fails mid-way
            for fallback_uri in _ALARM_URIS:
                if fallback_uri == working_uri:
                    continue
                if _insert_alarm(adb, device_id, fallback_uri, alarm):
                    inserted += 1
                    break
            else:
                logger.warning(
                    "Failed to inject alarm %02d:%02d '%s' on device %s.",
                    alarm.hour,
                    alarm.minute,
                    alarm.label,
                    device_id,
                )

    logger.info(
        "Injected %d / %d alarm(s) into device %s.",
        inserted,
        len(items),
        device_id,
    )
    return inserted
