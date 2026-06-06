"""
convert_alarms.py

Converts Alarm objects to/from dicts, and to readable strings / cron expressions.
"""

from __future__ import annotations

from core.normalization_schema import Alarm

# Day abbreviations for human-readable labels (ISO 0=Mon..6=Sun)
_DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def alarm_to_dict(alarm: Alarm) -> dict:
    """
    Serialize an Alarm to a plain dict.

    ``repeat_days`` is stored as ``list[int]`` (ISO weekday: 0=Mon…6=Sun).
    """
    return {
        "hour":        alarm.hour,
        "minute":      alarm.minute,
        "label":       alarm.label,
        "enabled":     alarm.enabled,
        "repeat_days": list(alarm.repeat_days),
        "sound":       alarm.sound,
    }


def dict_to_alarm(d: dict) -> Alarm:
    """
    Deserialize an Alarm from a plain dict.

    ``repeat_days`` defaults to an empty list if absent.
    """
    return Alarm(
        hour=int(d["hour"]),
        minute=int(d["minute"]),
        label=d.get("label", ""),
        enabled=bool(d.get("enabled", True)),
        repeat_days=list(d.get("repeat_days", [])),
        sound=d.get("sound"),
    )


# ---------------------------------------------------------------------------
# Cron expression
# ---------------------------------------------------------------------------

def alarm_to_cron(alarm: Alarm) -> str:
    """
    Return a cron expression for the alarm schedule.

    Format: ``"minute hour * * day_of_week_csv"``

    ISO weekdays (0=Mon…6=Sun) are mapped to cron weekdays (0=Sun…6=Sat)
    via ``(iso_day + 1) % 7``.

    If no repeat days are set, returns ``"minute hour * * *"`` (fire once /
    no recurrence in cron terms).
    """
    if not alarm.repeat_days:
        return f"{alarm.minute} {alarm.hour} * * *"

    cron_days = sorted({(d + 1) % 7 for d in alarm.repeat_days})
    return f"{alarm.minute} {alarm.hour} * * {','.join(str(d) for d in cron_days)}"


# ---------------------------------------------------------------------------
# Human-readable label
# ---------------------------------------------------------------------------

def alarm_label(alarm: Alarm) -> str:
    """
    Build a human-readable description of the alarm.

    Examples:
    - ``"07:30 Mon Wed Fri — Wake Up"``
    - ``"09:00 Daily — Morning"``
    - ``"08:00 Once"``
    - ``"22:45"``  (no label, no repeat)
    """
    time_str = f"{alarm.hour:02d}:{alarm.minute:02d}"
    label_part = f" \u2014 {alarm.label}" if alarm.label else ""

    if not alarm.repeat_days:
        return f"{time_str} Once{label_part}"

    # All seven days = Daily
    if set(alarm.repeat_days) == set(range(7)):
        return f"{time_str} Daily{label_part}"

    # Weekdays only (Mon–Fri)
    if set(alarm.repeat_days) == {0, 1, 2, 3, 4}:
        return f"{time_str} Weekdays{label_part}"

    # Weekends only (Sat–Sun)
    if set(alarm.repeat_days) == {5, 6}:
        return f"{time_str} Weekends{label_part}"

    day_names = " ".join(_DAY_ABBR[d] for d in sorted(alarm.repeat_days))
    return f"{time_str} {day_names}{label_part}"


# ---------------------------------------------------------------------------
# Merge helper
# ---------------------------------------------------------------------------

def merge_alarms(existing: list[Alarm], incoming: list[Alarm]) -> list[Alarm]:
    """
    Append alarms from *incoming* that are not already present in *existing*.

    Duplicate detection: same ``(hour, minute, frozenset(repeat_days))``.

    Returns the merged list (existing items first, then new ones).
    """
    existing_keys: set[tuple] = {
        (a.hour, a.minute, frozenset(a.repeat_days)) for a in existing
    }
    result = list(existing)
    for alarm in incoming:
        key = (alarm.hour, alarm.minute, frozenset(alarm.repeat_days))
        if key not in existing_keys:
            existing_keys.add(key)
            result.append(alarm)
    return result
