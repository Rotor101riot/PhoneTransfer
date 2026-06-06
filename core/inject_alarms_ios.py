"""
inject_alarms_ios.py

Inject Clock-app alarms onto an iOS device.

Two paths:
  * **Backup-mod (default)**: when an :class:`IOSBackupInjector` session is
    active, stage ``HomeDomain:Library/Preferences/com.apple.mobiletimerd.plist``
    and append synthetic alarm dicts to ``MTAlarms.MTAlarms`` (the iOS 17
    schema — each entry is wrapped in a ``{'$MTAlarm': {...}}`` envelope).
  * **AFC2 (legacy)**: pull/push the legacy
    ``com.apple.mobiletimer.plist`` through AFC2 on a jailbroken device.
    Kept as a fallback for the rare case where the caller intentionally
    bypassed the backup-mod orchestrator or is running an older iOS where
    the legacy plist is still authoritative.

Non-jailbroken without a backup session: writes a human-readable export
file and returns 0.
"""

from __future__ import annotations

import logging
import plistlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.ios_backup_injector import IOSBackupInjector, get_current_injector
from core.normalization_schema import Alarm

logger = logging.getLogger(__name__)

# Backup-mod target — iOS 17+ alarm storage.
_TIMERD_DOMAIN = "HomeDomain"
_TIMERD_RELPATH = "Library/Preferences/com.apple.mobiletimerd.plist"

# Legacy AFC2-only path (older iOS).
_LEGACY_DEVICE_PATH = "/var/mobile/Library/Preferences/com.apple.mobiletimer.plist"

_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def inject(
    device_id: str, items: list[Alarm], staging_dir: Path, is_privileged: bool
) -> int:
    if not items:
        logger.info("No alarms to inject for device %s.", device_id)
        return 0

    injector = get_current_injector()
    if injector is not None:
        try:
            count = _inject_via_backup(injector, items)
            logger.info(
                "inject_alarms_ios: staged %d alarm(s) into the backup for %s",
                count, device_id,
            )
            return count
        except Exception as exc:
            logger.warning(
                "inject_alarms_ios: backup-mod path failed (%s) — "
                "falling back to AFC2 / export", exc,
            )

    if not is_privileged:
        export_path = staging_dir / "alarms_ios.txt"
        logger.warning(
            "Cannot inject alarms without jailbreak or backup session. "
            "Alarms saved to %s for manual reference.", export_path,
        )
        _write_text_export(items, staging_dir)
        return 0

    return _inject_afc2_legacy(device_id, items)


# ---------------------------------------------------------------------------
# Backup-mod path (iOS 17+)
# ---------------------------------------------------------------------------

def _inject_via_backup(
    injector: IOSBackupInjector, items: list[Alarm]
) -> int:
    plist_local = injector.stage_db(_TIMERD_DOMAIN, _TIMERD_RELPATH)
    pl = plistlib.loads(plist_local.read_bytes())

    container = pl.get("MTAlarms")
    if not isinstance(container, dict):
        container = {"MTAlarms": [], "MTSleepAlarms": []}
        pl["MTAlarms"] = container
    user_alarms = container.setdefault("MTAlarms", [])

    existing_keys: set[tuple[int, int, str]] = set()
    for env in user_alarms:
        a = env.get("$MTAlarm") if isinstance(env, dict) else None
        if isinstance(a, dict):
            existing_keys.add((
                int(a.get("MTAlarmHour", -1)),
                int(a.get("MTAlarmMinute", -1)),
                str(a.get("MTAlarmTitle", "")),
            ))

    added = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for alarm in items:
        title = alarm.label or "Alarm"
        key = (alarm.hour, alarm.minute, title)
        if key in existing_keys:
            continue
        user_alarms.append({"$MTAlarm": _build_mtalarm_dict(alarm, now)})
        existing_keys.add(key)
        added += 1

    if added:
        pl["MTAlarmModifiedDate"] = now
        plist_local.write_bytes(plistlib.dumps(pl, fmt=plistlib.FMT_BINARY))

    return added


def _build_mtalarm_dict(alarm: Alarm, now: datetime) -> dict:
    """Construct a single iOS 17 ``$MTAlarm`` dict from an Alarm object."""
    fire_date = _next_fire_date(alarm.hour, alarm.minute, now)
    repeat_bitmask = _repeat_bitmask(alarm.repeat_days)

    sound_tone = alarm.sound or "Radar"
    return {
        "MTAlarmID": str(uuid.uuid4()).upper(),
        "MTAlarmHour": int(alarm.hour),
        "MTAlarmMinute": int(alarm.minute),
        "MTAlarmTitle": alarm.label or "Alarm",
        "MTAlarmEnabled": bool(alarm.enabled),
        "MTAlarmRepeatSchedule": repeat_bitmask,
        "MTAlarmFireDate": fire_date,
        "MTAlarmLastModifiedDate": now,
        "MTAlarmDataVersion": 6.0,
        "MTAlarmAllowsSnooze": True,
        "MTAlarmSnoozeDuration": 9,
        "MTAlarmSilentModeOptions": 2,
        "MTAlarmDismissAction": 0,
        "MTAlarmBedtimeDoNotDisturb": False,
        "MTAlarmBedtimeDoNotDisturbOptions": 0,
        "MTAlarmBedtimeDismissAction": 0,
        "MTAlarmBedtimeHour": 0,
        "MTAlarmBedtimeMinute": 0,
        "MTAlarmIsSleep": False,
        "MTAlarmSleepScheduleKey": False,
        "MTAlarmSleepTrackingKey": False,
        "MTAlarmTimeInBedTrackingKey": False,
        "MTAlarmCoordinationPolicy": 0,
        "MTAlarmOnboardingVersion": 0,
        "MTAlarmSound": {
            "$MTSound": {
                "MTSoundType": 2,
                "MTSoundToneID": f"system:{sound_tone}",
                "MTSoundVibrationID": f"synchronizedvibration:{sound_tone}",
            }
        },
    }


def _repeat_bitmask(repeat_days: list[int]) -> int:
    """Convert ISO weekdays (0=Mon..6=Sun) to Apple bitmask (bit 0=Sun..6=Sat)."""
    mask = 0
    for d in repeat_days or []:
        # ISO 0..6 (Mon..Sun) -> Apple 0..6 (Sun..Sat)
        apple = (d + 1) % 7
        mask |= 1 << apple
    return mask


def _next_fire_date(hour: int, minute: int, now: datetime) -> datetime:
    """Pick the next wall-clock instant matching hour:minute (today or tomorrow)."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


# ---------------------------------------------------------------------------
# Legacy AFC2 path (older iOS, jailbroken)
# ---------------------------------------------------------------------------

def _inject_afc2_legacy(device_id: str, items: list[Alarm]) -> int:
    try:
        from core.device_connection_cache import get_broker
        from core.afc2_connector import AFC2Connector

        broker = get_broker(device_id)
        afc2 = AFC2Connector(broker)

        plist_bytes = afc2.read_file(_LEGACY_DEVICE_PATH)
        if plist_bytes:
            try:
                data = plistlib.loads(plist_bytes)
            except Exception as exc:
                logger.warning(
                    "Could not parse legacy mobiletimer plist (%s); starting fresh.",
                    exc,
                )
                data = {"Alarms": []}
        else:
            data = {"Alarms": []}

        existing = data.setdefault("Alarms", [])
        added = 0
        for alarm in items:
            if any(_legacy_duplicate(ex, alarm) for ex in existing):
                continue
            existing.append(_legacy_alarm_to_dict(alarm))
            added += 1

        afc2.write_file(_LEGACY_DEVICE_PATH, plistlib.dumps(data, fmt=plistlib.FMT_XML))
        logger.info(
            "Injected %d alarm(s) into %s on device %s.",
            added, _LEGACY_DEVICE_PATH, device_id,
        )
        return added
    except Exception as exc:
        logger.error("Failed to inject alarms into device %s: %s", device_id, exc)
        return 0


def _legacy_alarm_to_dict(alarm: Alarm) -> dict:
    apple_days = [(d + 1) % 7 for d in (alarm.repeat_days or [])]
    return {
        "time": float(alarm.hour * 3600 + alarm.minute * 60),
        "title": alarm.label or "Alarm",
        "enabled": alarm.enabled,
        "repeatDays": apple_days,
        "snooze": True,
        "sound": alarm.sound or "Radar",
    }


def _legacy_duplicate(existing: dict, alarm: Alarm) -> bool:
    time_secs = float(existing.get("time", -1))
    return (
        int(time_secs // 3600) == alarm.hour
        and int((time_secs % 3600) // 60) == alarm.minute
        and existing.get("title", "") == (alarm.label or "Alarm")
    )


# ---------------------------------------------------------------------------
# Text export (last-resort fallback)
# ---------------------------------------------------------------------------

def _write_text_export(items: list[Alarm], staging_dir: Path) -> None:
    export_path = staging_dir / "alarms_ios.txt"
    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
        lines = ["iOS Alarms Export (PhoneTransfer)\n", "=" * 40 + "\n"]
        for i, alarm in enumerate(items, 1):
            days_str = (
                ", ".join(_DAY_NAMES[d] for d in sorted(alarm.repeat_days))
                if alarm.repeat_days else "Once"
            )
            lines.append(
                f"{i:3}. {alarm.hour:02d}:{alarm.minute:02d}  "
                f"Label: {alarm.label or '(none)'}  "
                f"Repeat: {days_str}  "
                f"Enabled: {alarm.enabled}  "
                f"Sound: {alarm.sound or 'Radar'}\n"
            )
        export_path.write_text("".join(lines), encoding="utf-8")
        logger.info("Alarm export written to %s", export_path)
    except Exception as exc:
        logger.warning("Could not write alarm text export: %s", exc)
