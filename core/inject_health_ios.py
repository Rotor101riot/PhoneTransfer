"""
inject_health_ios.py

Injects health and fitness records into an iOS device.

iOS HealthKit data lives inside a protected database (healthdb.sqlite or
healthdb_secure.sqlite) that can only be written by:
  - HealthKit-enabled apps at runtime (the standard API).
  - Jailbroken devices: direct write via AFC2, but this is risky because
    Apple does not document the internal schema and it changes across iOS
    versions.

This module therefore:
  1. On jailbroken devices: attempts direct insertion into healthdb.sqlite
     using the best-known schema reverse-engineered for iOS 15–17.
  2. On all devices: exports an Apple Health XML archive (.xml) that conforms
     to the format produced by "Health → Profile → Export All Health Data".
     iOS 16+ allows importing such files by tapping them in the Files app.

Returns the count of records injected directly to the device (0 for
non-jailbroken or when the schema is incompatible).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from core.normalization_schema import HealthRecord

logger = logging.getLogger(__name__)

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_HEALTHDB_PATH = "/var/mobile/Library/Health/healthdb.sqlite"

# Map our normalized category → HealthKit HKQuantityTypeIdentifier + unit string
_CAT_TO_HK: dict[str, tuple[str, str]] = {
    "steps":                   ("HKQuantityTypeIdentifierStepCount",              "count"),
    "heart_rate":              ("HKQuantityTypeIdentifierHeartRate",               "count/min"),
    "calories":                ("HKQuantityTypeIdentifierActiveEnergyBurned",      "kcal"),
    "weight":                  ("HKQuantityTypeIdentifierBodyMass",                "kg"),
    "height":                  ("HKQuantityTypeIdentifierHeight",                  "m"),
    "blood_glucose":           ("HKQuantityTypeIdentifierBloodGlucose",            "mg/dL"),
    "oxygen_saturation":       ("HKQuantityTypeIdentifierOxygenSaturation",        "%"),
    "blood_pressure_systolic": ("HKQuantityTypeIdentifierBloodPressureSystolic",   "mmHg"),
    "blood_pressure_diastolic":("HKQuantityTypeIdentifierBloodPressureDiastolic",  "mmHg"),
    "body_temperature":        ("HKQuantityTypeIdentifierBodyTemperature",         "degC"),
    "distance":                ("HKQuantityTypeIdentifierDistanceWalkingRunning",  "m"),
    "floors_climbed":          ("HKQuantityTypeIdentifierFlightsClimbed",          "count"),
    "resting_heart_rate":      ("HKQuantityTypeIdentifierRestingHeartRate",        "count/min"),
    "workout":                 ("HKWorkoutTypeIdentifier",                         "kcal"),
    "sleep":                   ("HKCategoryTypeIdentifierSleepAnalysis",           "min"),
}

_DATE_FMT = "%Y-%m-%d %H:%M:%S %z"


def inject(
    device_id: str,
    items: list[HealthRecord],
    staging_dir: Path,
    is_privileged: bool,
) -> int:
    if not items:
        return 0

    staging_dir.mkdir(parents=True, exist_ok=True)

    # Always export an Apple Health XML archive
    _export_xml(items, staging_dir)

    if not is_privileged:
        logger.warning(
            "[health/ios] Direct health data injection requires a jailbroken device. "
            "An Apple Health XML export of %d record(s) has been written to %s. "
            "On iOS 16+, open this file in the Files app and tap 'Import to Health' "
            "to import it natively.",
            len(items),
            staging_dir / "health_ios_export.xml",
        )
        return 0

    return _inject_jailbroken(device_id, items, staging_dir)


# ---------------------------------------------------------------------------
# Jailbroken injection via AFC2
# ---------------------------------------------------------------------------

def _inject_jailbroken(
    device_id: str,
    items: list[HealthRecord],
    staging_dir: Path,
) -> int:
    try:
        from core.afc2_connector import AFC2Connector
    except ImportError:
        logger.error("[health/ios] AFC2Connector not available")
        return 0

    local_db = staging_dir / "healthdb_inject.sqlite"

    # Pull existing healthdb.sqlite
    try:
        with AFC2Connector(device_id) as afc2:
            data = afc2.read_file(_HEALTHDB_PATH)
        if not data:
            logger.warning(
                "[health/ios] healthdb.sqlite is empty or inaccessible on %s. "
                "It may be encrypted (healthdb_secure.sqlite) which cannot be "
                "written without the device keychain key.",
                device_id,
            )
            return 0
        local_db.write_bytes(data)
    except Exception as exc:
        logger.error("[health/ios] Failed to pull healthdb.sqlite: %s", exc)
        return 0

    inserted = _insert_records(local_db, items)
    if inserted == 0:
        return 0

    # Push back
    try:
        with AFC2Connector(device_id) as afc2:
            afc2.write_file(_HEALTHDB_PATH, local_db.read_bytes())
    except Exception as exc:
        logger.error("[health/ios] Failed to write healthdb.sqlite back to device: %s", exc)
        return 0

    logger.info(
        "[health/ios] Injected %d health record(s) into %s. "
        "Force-quit the Health app to see new data.",
        inserted, device_id,
    )
    return inserted


def _insert_records(db_path: Path, records: list[HealthRecord]) -> int:
    """
    Insert HealthRecord objects into healthdb.sqlite using the known iOS schema.

    Tables written:
      sources           — recording app name
      source_revisions  — links source to a version
      samples           — one row per measurement (data_type, start_date, end_date)
      quantity_samples  — numeric value for quantity types
      data_types        — HKTypeIdentifier → integer ID mapping

    NOTE: The healthdb schema is Apple-internal and may vary across iOS versions.
    This implementation targets iOS 15–17 schema layouts.
    """
    inserted = 0
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            type_map   = _ensure_data_types(conn, records)
            source_id  = _ensure_source(conn, "PhoneTransfer")

            for rec in records:
                hk_type, _ = _CAT_TO_HK.get(rec.category, (None, None))
                if hk_type is None:
                    continue
                type_id = type_map.get(hk_type)
                if type_id is None:
                    continue
                try:
                    _insert_sample(conn, rec, type_id, source_id)
                    inserted += 1
                except Exception as exc:
                    logger.debug("[health/ios] Skipping record %s: %s", rec.category, exc)

            conn.commit()
    except Exception as exc:
        logger.error("[health/ios] DB write error: %s", exc)

    return inserted


def _ensure_data_types(
    conn: sqlite3.Connection,
    records: list[HealthRecord],
) -> dict[str, int]:
    """
    Ensure all needed HKTypeIdentifier rows exist in the data_types table.
    Returns a mapping of identifier → primary_key.
    """
    needed_hk = set()
    for rec in records:
        hk_type, _ = _CAT_TO_HK.get(rec.category, (None, None))
        if hk_type:
            needed_hk.add(hk_type)

    result: dict[str, int] = {}
    try:
        cur = conn.execute("SELECT primary_key, identifier FROM data_types")
        for row in cur.fetchall():
            result[row["identifier"]] = row["primary_key"]
    except sqlite3.OperationalError:
        pass

    for hk in needed_hk:
        if hk not in result:
            try:
                c = conn.execute(
                    "INSERT INTO data_types (identifier) VALUES (?)", (hk,)
                )
                result[hk] = c.lastrowid
            except Exception as exc:
                logger.debug("[health/ios] Could not insert data_type %s: %s", hk, exc)

    return result


def _ensure_source(conn: sqlite3.Connection, name: str) -> int:
    """Ensure a 'sources' row exists for our app and return its primary_key."""
    try:
        row = conn.execute(
            "SELECT primary_key FROM sources WHERE name = ?", (name,)
        ).fetchone()
        if row:
            return row["primary_key"]
        c = conn.execute("INSERT INTO sources (name) VALUES (?)", (name,))
        return c.lastrowid
    except sqlite3.OperationalError:
        return 1   # fallback to first source


def _insert_sample(
    conn: sqlite3.Connection,
    rec: HealthRecord,
    type_id: int,
    source_id: int,
) -> None:
    start_apple = _to_apple_epoch(rec.start)
    end_apple   = _to_apple_epoch(rec.end) if rec.end else start_apple + 60.0

    c = conn.execute(
        "INSERT INTO samples (data_type, start_date, end_date, data_id, source_id) "
        "VALUES (?, ?, ?, NULL, ?)",
        (type_id, start_apple, end_apple, source_id),
    )
    data_id = c.lastrowid

    if rec.category != "sleep":
        conn.execute(
            "INSERT INTO quantity_samples (data_id, quantity) VALUES (?, ?)",
            (data_id, rec.value),
        )
    else:
        # SleepAnalysis: value 1 = Asleep
        conn.execute(
            "INSERT INTO category_samples (data_id, value) VALUES (?, ?)",
            (data_id, 1),
        )


# ---------------------------------------------------------------------------
# Apple Health XML export
# ---------------------------------------------------------------------------

def _export_xml(records: list[HealthRecord], staging_dir: Path) -> None:
    """
    Write an Apple Health XML export compatible with iOS 16+ native import.

    Format:
      <HealthData locale="en_US">
        <Record type="HKQuantityTypeIdentifierStepCount"
                sourceName="PhoneTransfer" sourceVersion="1.0"
                unit="count"
                creationDate="2024-01-01 00:00:00 +0000"
                startDate="2024-01-01 00:00:00 +0000"
                endDate="2024-01-01 01:00:00 +0000"
                value="8000"/>
      </HealthData>
    """
    root = ET.Element("HealthData", locale="en_US")

    for rec in records:
        hk_type, hk_unit = _CAT_TO_HK.get(rec.category, (None, None))
        if hk_type is None:
            continue

        end_dt = rec.end or rec.start
        attrs = {
            "type":          hk_type,
            "sourceName":    rec.source_name or "PhoneTransfer",
            "sourceVersion": "1.0",
            "unit":          hk_unit,
            "creationDate":  rec.start.strftime(_DATE_FMT),
            "startDate":     rec.start.strftime(_DATE_FMT),
            "endDate":       end_dt.strftime(_DATE_FMT),
            "value":         str(rec.value),
        }
        ET.SubElement(root, "Record", **attrs)

    out = staging_dir / "health_ios_export.xml"
    try:
        tree = ET.ElementTree(root)
        ET.indent(tree, space="  ")
        with out.open("wb") as f:
            f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
            tree.write(f, encoding="utf-8", xml_declaration=False)
        logger.info("[health/ios] Apple Health XML export: %s (%d records)", out, len(records))
    except Exception as exc:
        logger.warning("[health/ios] XML export failed: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_apple_epoch(dt: datetime) -> float:
    """Convert a UTC datetime to seconds since 2001-01-01 (Apple epoch)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - _APPLE_EPOCH).total_seconds()
