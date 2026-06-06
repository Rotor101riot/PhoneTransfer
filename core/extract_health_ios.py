"""
extract_health_ios.py

Extracts health and fitness data from an iOS device.

Access paths:
  - Jailbroken:     Pull /var/mobile/Library/Health/healthdb.sqlite via AFC2
                    and parse the HealthKit SQLite schema.
  - Non-jailbroken: Attempt to retrieve the health database from an
                    iTunes/Finder backup via iOSbackup.

Returns a list of HealthRecord objects as defined in normalization_schema.py.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.normalization_schema import HealthRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Apple epoch
# ---------------------------------------------------------------------------

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Device paths
# ---------------------------------------------------------------------------

_HEALTHDB_PATH        = "/var/mobile/Library/Health/healthdb.sqlite"
_HEALTHDB_SECURE_PATH = "/var/mobile/Library/Health/healthdb_secure.sqlite"

# iOSbackup domain / relative path
_IOSBACKUP_DOMAIN   = "HealthDomain"
_IOSBACKUP_RELATIVE = "Library/Health/healthdb.sqlite"

# Map iOS HealthKit type identifier fragments → normalized category name + unit
_HKTYPE_MAP: dict[str, tuple[str, str]] = {
    "StepCount":                 ("steps",                   "count"),
    "HeartRate":                 ("heart_rate",               "bpm"),
    "ActiveEnergyBurned":        ("calories",                 "kcal"),
    "BasalEnergyBurned":         ("calories",                 "kcal"),
    "BodyMass":                  ("weight",                   "kg"),
    "Height":                    ("height",                   "m"),
    "BloodGlucose":              ("blood_glucose",            "mg/dL"),
    "OxygenSaturation":          ("oxygen_saturation",        "%"),
    "BloodPressureSystolic":     ("blood_pressure_systolic",  "mmHg"),
    "BloodPressureDiastolic":    ("blood_pressure_diastolic", "mmHg"),
    "BodyTemperature":           ("body_temperature",         "°C"),
    "DistanceWalkingRunning":    ("distance",                 "m"),
    "FlightsClimbed":            ("floors_climbed",           "count"),
    "RestingHeartRate":          ("resting_heart_rate",       "bpm"),
    "SleepAnalysis":             ("sleep",                    "min"),
}

_SUBDIR = "health_ios"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(
    udid: str,
    staging_dir: Path,
    is_jailbroken: bool = False,
) -> list[HealthRecord]:
    try:
        return _extract_impl(udid, staging_dir, is_jailbroken)
    except Exception:
        logger.exception("[health/ios] Unhandled error during extraction")
        return []


def _extract_impl(
    udid: str,
    staging_dir: Path,
    is_jailbroken: bool,
) -> list[HealthRecord]:
    sub = staging_dir / _SUBDIR
    sub.mkdir(parents=True, exist_ok=True)

    db_path = _obtain_db(udid, sub, is_jailbroken)
    if db_path is None:
        logger.warning(
            "[health/ios] Could not obtain healthdb.sqlite for %s. "
            "Jailbroken devices need AFC2; non-jailbroken devices need a local "
            "iTunes/Finder backup with health data included.",
            udid,
        )
        return []

    records = _parse_healthdb(db_path)
    logger.info("[health/ios] Extracted %d health records for %s", len(records), udid)
    return records


# ---------------------------------------------------------------------------
# Obtain healthdb.sqlite
# ---------------------------------------------------------------------------

def _obtain_db(udid: str, sub: Path, is_jailbroken: bool) -> Path | None:
    dest = sub / "healthdb.sqlite"

    if is_jailbroken:
        result = _pull_via_afc2(udid, dest)
        if result is not None:
            return result
        logger.warning("[health/ios] AFC2 pull failed; trying iOSbackup")

    return _pull_via_iosbackup(udid, dest)


def _pull_via_afc2(udid: str, dest: Path) -> Path | None:
    try:
        from core.afc2_connector import AFC2Connector
    except ImportError as exc:
        logger.warning("[health/ios] AFC2 not available: %s", exc)
        return None

    for remote in (_HEALTHDB_PATH, _HEALTHDB_SECURE_PATH):
        try:
            with AFC2Connector(udid) as afc2:
                data = afc2.read_file(remote)
            if data and len(data) > 100:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                logger.debug("[health/ios] Pulled %s via AFC2", remote)
                return dest
        except Exception as exc:
            logger.debug("[health/ios] AFC2 read %s failed: %s", remote, exc)

    return None


def _pull_via_iosbackup(udid: str, dest: Path) -> Path | None:
    try:
        from core.device_connection_cache import get_iosbackup
        dest.parent.mkdir(parents=True, exist_ok=True)
        backup = get_iosbackup(udid)
        info = backup.getFileDecryptedCopy(
            relativePath=_IOSBACKUP_RELATIVE,
            targetName=dest.name,
            targetFolder=str(dest.parent),
        )
        if not info or not dest.exists():
            logger.warning(
                "[health/ios] iOSbackup returned no data for %s/%s on %s",
                _IOSBACKUP_DOMAIN, _IOSBACKUP_RELATIVE, udid,
            )
            return None

        logger.debug("[health/ios] Pulled healthdb.sqlite via iOSbackup")
        return dest
    except Exception as exc:
        logger.warning("[health/ios] iOSbackup pull failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Parse healthdb.sqlite
# ---------------------------------------------------------------------------

def _parse_healthdb(db_path: Path) -> list[HealthRecord]:
    """
    Parse the iOS HealthKit SQLite database.

    Primary tables (schema is Apple-internal but well-documented in forensics):
      samples           — one row per measurement (start_date, end_date, data_type)
      quantity_samples  — stores numeric value for quantity types
      category_samples  — stores integer value for category types (e.g. sleep)
      data_types        — maps integer type ID to HK identifier string
      sources           — name of recording app/device
    """
    records: list[HealthRecord] = []

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            type_map  = _build_data_type_map(conn)
            source_map = _build_source_map(conn)
            records.extend(_parse_quantity_samples(conn, type_map, source_map))
            records.extend(_parse_category_samples(conn, type_map, source_map))
            records.extend(_parse_workouts(conn))
    except Exception:
        logger.exception("[health/ios] Failed to parse healthdb.sqlite")

    return records


def _build_data_type_map(conn: sqlite3.Connection) -> dict[int, str]:
    """Map data_type integer → HKTypeIdentifier string."""
    result: dict[int, str] = {}
    for table in ("data_types", "type_definitions"):
        try:
            cur = conn.execute(f"SELECT primary_key, identifier FROM {table}")
            for row in cur.fetchall():
                result[row["primary_key"]] = row["identifier"] or ""
            break
        except sqlite3.OperationalError:
            continue
    return result


def _build_source_map(conn: sqlite3.Connection) -> dict[int, str]:
    """Map source integer → source name string."""
    result: dict[int, str] = {}
    for table in ("sources", "source_revisions"):
        try:
            cur = conn.execute("SELECT primary_key, name FROM sources")
            for row in cur.fetchall():
                result[row["primary_key"]] = row["name"] or ""
            break
        except sqlite3.OperationalError:
            continue
    return result


def _parse_quantity_samples(
    conn: sqlite3.Connection,
    type_map: dict[int, str],
    source_map: dict[int, str],
) -> list[HealthRecord]:
    records = []
    try:
        # samples JOIN quantity_samples
        cur = conn.execute(
            """
            SELECT s.data_type, s.start_date, s.end_date,
                   q.quantity, s.source_id
            FROM samples AS s
            JOIN quantity_samples AS q ON q.data_id = s.data_id
            ORDER BY s.start_date ASC
            """
        )
        for row in cur.fetchall():
            identifier = type_map.get(row["data_type"], "")
            cat, unit  = _resolve_hktype(identifier)
            if cat is None:
                continue

            value = float(row["quantity"] or 0)
            # Some values stored in SI units; convert for readability
            if cat == "weight":
                value = round(value, 2)          # kg (already SI)
            elif cat == "height":
                value = round(value, 3)          # m (already SI)
            elif cat == "oxygen_saturation":
                value = round(value * 100, 1)    # fraction → percent

            records.append(HealthRecord(
                category    = cat,
                value       = value,
                unit        = unit,
                start       = _apple_ts(row["start_date"]),
                end         = _apple_ts(row["end_date"]) if row["end_date"] else None,
                source_name = source_map.get(row["source_id"]),
            ))
    except sqlite3.OperationalError as exc:
        logger.debug("[health/ios] quantity_samples query failed: %s", exc)

    return records


def _parse_category_samples(
    conn: sqlite3.Connection,
    type_map: dict[int, str],
    source_map: dict[int, str],
) -> list[HealthRecord]:
    """
    Category samples — mainly sleep analysis.
    SleepAnalysis values: 0=InBed, 1=Asleep, 2=Awake, 3=CoreSleep, 4=DeepSleep, 5=RemSleep.
    We record duration for each Asleep/sleep-stage window.
    """
    records = []
    try:
        cur = conn.execute(
            """
            SELECT s.data_type, s.start_date, s.end_date,
                   c.value, s.source_id
            FROM samples AS s
            JOIN category_samples AS c ON c.data_id = s.data_id
            ORDER BY s.start_date ASC
            """
        )
        for row in cur.fetchall():
            identifier = type_map.get(row["data_type"], "")
            if "Sleep" not in identifier:
                continue
            start = _apple_ts(row["start_date"])
            end   = _apple_ts(row["end_date"]) if row["end_date"] else None
            dur_min = (end - start).total_seconds() / 60 if end else 0
            sleep_stage = {0: "in_bed", 1: "asleep", 2: "awake",
                           3: "core", 4: "deep", 5: "rem"}.get(row["value"], "unknown")
            records.append(HealthRecord(
                category    = "sleep",
                value       = round(dur_min, 1),
                unit        = "min",
                start       = start,
                end         = end,
                source_name = source_map.get(row["source_id"]),
                notes       = sleep_stage,
            ))
    except sqlite3.OperationalError as exc:
        logger.debug("[health/ios] category_samples query failed: %s", exc)

    return records


def _parse_workouts(conn: sqlite3.Connection) -> list[HealthRecord]:
    records = []
    try:
        cur = conn.execute(
            """
            SELECT start_date, end_date, workout_type,
                   total_energy_burned, total_distance
            FROM workouts ORDER BY start_date ASC
            """
        )
        for row in cur.fetchall():
            start = _apple_ts(row["start_date"])
            end   = _apple_ts(row["end_date"]) if row["end_date"] else None
            records.append(HealthRecord(
                category    = "workout",
                value       = float(row["total_energy_burned"] or 0),
                unit        = "kcal",
                start       = start,
                end         = end,
                source_name = "Apple Health",
                notes       = f"workout_type={row['workout_type']}",
            ))
    except sqlite3.OperationalError as exc:
        logger.debug("[health/ios] workouts query failed: %s", exc)
    return records


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_hktype(identifier: str) -> tuple[str | None, str]:
    """Return (category, unit) for a HealthKit type identifier, or (None, '')."""
    for fragment, (cat, unit) in _HKTYPE_MAP.items():
        if fragment in identifier:
            return cat, unit
    return None, ""


def _apple_ts(ts: float | int | None) -> datetime:
    if ts is None or ts == 0:
        return _APPLE_EPOCH
    try:
        return _APPLE_EPOCH + timedelta(seconds=float(ts))
    except (OverflowError, OSError, ValueError):
        return _APPLE_EPOCH
