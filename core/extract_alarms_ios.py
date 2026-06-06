from __future__ import annotations

import logging
import plistlib
from pathlib import Path

from core.normalization_schema import Alarm

logger = logging.getLogger(__name__)


def _apple_day_to_iso(apple_day: int) -> int:
    """Convert Apple repeat day (0=Sun..6=Sat) to ISO (0=Mon..6=Sun)."""
    # Apple: 0=Sun,1=Mon,...,6=Sat
    # ISO:   0=Mon,1=Tue,...,6=Sun
    # Sun(0) -> 6, Mon(1) -> 0, Tue(2) -> 1, ..., Sat(6) -> 5
    return (apple_day - 1) % 7


def _parse_plist_alarms(plist_data: bytes) -> list[Alarm]:
    """Parse plist bytes and return list of Alarm objects."""
    data = plistlib.loads(plist_data)
    raw_alarms = data.get("Alarms", [])
    alarms: list[Alarm] = []
    for entry in raw_alarms:
        try:
            time_secs = float(entry.get("time", 0))
            hour = int(time_secs // 3600)
            minute = int((time_secs % 3600) // 60)
            label = entry.get("title", "")
            enabled = bool(entry.get("enabled", True))
            sound = entry.get("sound") or None
            raw_days = entry.get("repeatDays", [])
            repeat_days = [_apple_day_to_iso(d) for d in raw_days]
            alarms.append(
                Alarm(
                    hour=hour,
                    minute=minute,
                    label=label,
                    enabled=enabled,
                    repeat_days=repeat_days,
                    sound=sound,
                )
            )
        except Exception as exc:
            logger.warning("Skipping malformed alarm entry %r: %s", entry, exc)
    return alarms


def extract(device_id: str, staging_dir: Path, is_privileged: bool) -> list[Alarm]:
    """Extract alarms from an iOS device.

    Args:
        device_id: iOS UDID.
        staging_dir: Local directory used for temporary files.
        is_privileged: True if the device is jailbroken (AFC2 available).

    Returns:
        A list of Alarm objects, or [] on failure.
    """
    plist_path = "Library/Preferences/com.apple.mobiletimer.plist"

    if is_privileged:
        # Jailbroken path — read via AFC2
        try:
            from pymobiledevice3.services.afc import AfcService
            from core.device_connection_cache import get_lockdown

            lockdown = get_lockdown(device_id)
            # Use com.apple.afc2 service for jailbroken devices
            afc2 = AfcService(lockdown=lockdown, service_name="com.apple.afc2")
            device_plist_path = f"/var/mobile/{plist_path}"
            plist_bytes = afc2.get_file_contents(device_plist_path)
            logger.info(
                "Read mobiletimer plist via AFC2 for device %s (%d bytes)",
                device_id,
                len(plist_bytes),
            )
            return _parse_plist_alarms(plist_bytes)
        except Exception as exc:
            logger.error(
                "AFC2 read of mobiletimer plist failed for device %s: %s",
                device_id,
                exc,
            )
            return []
    else:
        # Non-jailbroken path — use iOSbackup
        try:
            from core.device_connection_cache import get_iosbackup

            backup = get_iosbackup(device_id)
            _result = backup.getRelativePathDecryptedData(
                relativePath=plist_path,
            )
            # getRelativePathDecryptedData returns (info, bytes) for encrypted
            # backups and raw bytes for unencrypted ones.
            plist_bytes = _result[1] if isinstance(_result, tuple) else _result
            if not plist_bytes:
                logger.warning(
                    "mobiletimer plist not found in backup for device %s", device_id
                )
                return []
            logger.info(
                "Read mobiletimer plist via iOSbackup for device %s (%d bytes)",
                device_id,
                len(plist_bytes),
            )
            return _parse_plist_alarms(plist_bytes)
        except Exception as exc:
            logger.error(
                "iOSbackup extraction of mobiletimer plist failed for device %s: %s",
                device_id,
                exc,
            )
            return []
