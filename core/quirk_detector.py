"""
core/quirk_detector.py

Loads device_quirks.json and matches quirks against the source and
destination DeviceInfo objects for a pending transfer.

Usage
-----
    from core.quirk_detector import detect_quirks, Quirk

    pairs = detect_quirks(source_dev, dest_dev)
    # pairs is a list of (Quirk, "source" | "destination") tuples,
    # ordered source quirks first, then destination quirks.
    # Duplicates (same quirk applicable to both devices) appear twice,
    # once per role.

Never raises — returns an empty list on any failure.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from core.normalization_schema import DeviceInfo

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent.parent / "reference" / "device_quirks.json"

# ---------------------------------------------------------------------------
# Quirk dataclass
# ---------------------------------------------------------------------------

@dataclass
class Quirk:
    id:           str
    title:        str
    description:  str
    steps:        list[str]
    revert_steps: list[str]
    severity:     Literal["warning", "info"]
    # "source" / "destination" — set by the detector (not from JSON directly)
    device_role:  str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_version(ver: str) -> tuple[int, ...]:
    """
    Convert a version string like "17.4.1" or "14" to a comparable tuple.
    Non-numeric segments are treated as 0.
    """
    parts = []
    for segment in ver.split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _version_gte(device_ver: str, min_ver: str) -> bool:
    return _parse_version(device_ver) >= _parse_version(min_ver)


def _version_lte(device_ver: str, max_ver: str) -> bool:
    return _parse_version(device_ver) <= _parse_version(max_ver)


def _matches(entry: dict, dev: DeviceInfo) -> bool:
    """
    Return True if the device satisfies all conditions in the 'match' dict.

    Supported match keys:
      platform          "ios" | "android"
      brand_contains    list[str] — any element must appear in dev.brand (case-insensitive)
      os_version_min    str — device os_version must be >= this
      os_version_max    str — device os_version must be <= this
      is_jailbroken     bool
      is_rooted         bool
      model_prefix      list[str] — dev.model must start with one of these
    """
    m = entry.get("match", {})

    if "platform" in m and dev.platform != m["platform"]:
        return False

    if "brand_contains" in m:
        brand_lower = (dev.brand or "").lower()
        if not any(b.lower() in brand_lower for b in m["brand_contains"]):
            return False

    if "os_version_min" in m:
        try:
            if not _version_gte(dev.os_version, m["os_version_min"]):
                return False
        except Exception:
            pass  # unparseable version — skip check

    if "os_version_max" in m:
        try:
            if not _version_lte(dev.os_version, m["os_version_max"]):
                return False
        except Exception:
            pass

    if "is_jailbroken" in m:
        if dev.is_jailbroken != m["is_jailbroken"]:
            return False

    if "is_rooted" in m:
        if dev.is_rooted != m["is_rooted"]:
            return False

    if "model_prefix" in m:
        if not any(dev.model.startswith(p) for p in m["model_prefix"]):
            return False

    return True


def _load_db() -> list[dict]:
    try:
        raw = _DB_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data.get("quirks", [])
    except FileNotFoundError:
        logger.warning("quirk_detector: device_quirks.json not found at %s", _DB_PATH)
    except Exception as exc:
        logger.warning("quirk_detector: failed to load device_quirks.json: %s", exc)
    return []


def _entry_to_quirk(entry: dict, role: str) -> Quirk:
    return Quirk(
        id=entry.get("id", ""),
        title=entry.get("title", ""),
        description=entry.get("description", ""),
        steps=entry.get("steps", []),
        revert_steps=entry.get("revert_steps", []),
        severity=entry.get("severity", "info"),
        device_role=role,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_quirks(
    source: DeviceInfo,
    dest: DeviceInfo,
) -> list[tuple[Quirk, str]]:
    """
    Match all applicable quirks for the given source and destination devices.

    Returns a list of ``(Quirk, role)`` pairs where *role* is ``"source"`` or
    ``"destination"``.  The list is ordered: all source quirks first (in JSON
    order), then all destination quirks.

    Quirks with ``"applies_to": "either"`` are checked against both devices
    independently and produce separate entries per matching device.  Quirks
    with ``"applies_to": "source"`` only match the source device; ``"dest"``
    only the destination.

    Duplicate quirk IDs for the same device role are deduplicated.
    """
    try:
        return _detect_impl(source, dest)
    except Exception as exc:
        logger.exception("quirk_detector: unexpected error: %s", exc)
        return []


def _detect_impl(
    source: DeviceInfo,
    dest: DeviceInfo,
) -> list[tuple[Quirk, str]]:
    db = _load_db()
    result: list[tuple[Quirk, str]] = []
    seen: set[tuple[str, str]] = set()  # (quirk_id, role) deduplication

    for entry in db:
        applies_to = entry.get("applies_to", "either")

        # Determine which device roles to test
        checks: list[tuple[DeviceInfo, str]] = []
        if applies_to in ("either", "source"):
            checks.append((source, "source"))
        if applies_to in ("either", "destination"):
            checks.append((dest, "destination"))
        if applies_to == "both":
            # Must match BOTH devices for the quirk to fire at all
            if _matches(entry, source) and _matches(entry, dest):
                key_src  = (entry.get("id", ""), "source")
                key_dest = (entry.get("id", ""), "destination")
                if key_src not in seen:
                    seen.add(key_src)
                    result.append((_entry_to_quirk(entry, "source"), "source"))
                if key_dest not in seen:
                    seen.add(key_dest)
                    result.append((_entry_to_quirk(entry, "destination"), "destination"))
            continue

        for dev, role in checks:
            if _matches(entry, dev):
                key = (entry.get("id", ""), role)
                if key not in seen:
                    seen.add(key)
                    result.append((_entry_to_quirk(entry, role), role))

    return result


def has_blocking_quirks(pairs: list[tuple[Quirk, str]]) -> bool:
    """Return True if any matched quirk has severity 'warning'."""
    return any(q.severity == "warning" for q, _ in pairs)
