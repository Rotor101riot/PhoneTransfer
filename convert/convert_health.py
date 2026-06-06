"""
convert_health.py

Health/fitness data conversion stubs.

HealthKit (iOS) and Health Connect (Android) use different data models; a full
conversion is platform-specific and requires device-side access.  This module
provides serialization helpers for any health data that can be read via backup
parsing.

NOTE: Full iOS→Android health migration requires HealthKit entitlement on iOS
and Health Connect permissions on Android.  The pipeline logs a warning and
skips health data unless the device is jailbroken/rooted with appropriate
access.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_WARNED = False


def _warn_once() -> None:
    global _WARNED
    if not _WARNED:
        log.warning(
            "Health data migration is limited: full iOS→Android transfer requires "
            "HealthKit entitlement (iOS) and Health Connect permissions (Android). "
            "Health data will be skipped unless the device is jailbroken/rooted with "
            "appropriate access."
        )
        _WARNED = True


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class HealthSample:
    """A single health/fitness data point from HealthKit or Health Connect."""

    type: str          # e.g. "HKQuantityTypeIdentifierStepCount"
    start: datetime
    end: datetime
    value: float
    unit: str          # e.g. "count", "km", "kcal"
    source: str | None = None


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def sample_to_dict(s: HealthSample) -> dict:
    """Serialize a HealthSample to a plain dict (``datetime`` fields → ISO strings)."""
    _warn_once()
    return {
        "type":   s.type,
        "start":  s.start.isoformat(),
        "end":    s.end.isoformat(),
        "value":  s.value,
        "unit":   s.unit,
        "source": s.source,
    }


def dict_to_sample(d: dict) -> HealthSample:
    """Deserialize a HealthSample from a plain dict."""
    return HealthSample(
        type=d["type"],
        start=datetime.fromisoformat(d["start"]),
        end=datetime.fromisoformat(d["end"]),
        value=float(d["value"]),
        unit=d["unit"],
        source=d.get("source"),
    )


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def samples_to_json(samples: list[HealthSample], path: Path) -> Path:
    """
    Write a list of HealthSample objects to a JSON file at *path*.

    Returns *path*.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([sample_to_dict(s) for s in samples], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def samples_from_json(path: Path) -> list[HealthSample]:
    """
    Read and parse a JSON file written by :func:`samples_to_json`.

    Returns a list of HealthSample objects.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [dict_to_sample(d) for d in raw]


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------

def summarize(samples: list[HealthSample]) -> dict[str, int]:
    """
    Return a ``{type: count}`` summary of samples grouped by health data type.

    Useful for logging and validation before attempting migration.
    """
    counts: dict[str, int] = {}
    for s in samples:
        counts[s.type] = counts.get(s.type, 0) + 1
    return counts
