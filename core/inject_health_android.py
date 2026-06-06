"""
inject_health_android.py

Injects health and fitness records into an Android device.

Android HealthConnect (API 33+) is a system health data broker — it does not
expose a writable SQLite path.  The correct write path is through the
HealthConnect SDK API, which requires an app with READ_HEALTH_DATA and
WRITE_HEALTH_DATA permissions and explicit user consent per data type.

This module therefore uses a two-pronged approach:

  1. Rooted + Samsung Health: insert directly into SHealth7.db for the types
     we know the schema for (steps, heart rate, sleep, exercise, weight).
  2. All other cases: export a human-readable JSON file to staging that the
     user can import via a third-party migration app (e.g. Health Sync, Fit
     to Fit) or keep as an archive.

Returns the count of records actually written to the device (0 for non-rooted
or when the DB target is unavailable).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from pathlib import Path

from core.adb_manager import ADBManager
from core.config_loader import get_config
from core.normalization_schema import HealthRecord

logger = logging.getLogger(__name__)

_SHEALTH_PKG    = "com.sec.android.app.shealth"
_SHEALTH_DB_DEV = f"/data/data/{_SHEALTH_PKG}/databases/SHealth7.db"
_SHEALTH_SDCARD = "/sdcard/PT_shealth_inject.db"
_SHEALTH_SDCARD_PULL = "/sdcard/PT_shealth_pull.db"

# Categories this module can write into Samsung Health
_SAMSUNG_WRITABLE = {"steps", "heart_rate", "weight"}


def inject(
    device_id: str,
    items: list[HealthRecord],
    staging_dir: Path,
    is_privileged: bool,
) -> int:
    if not items:
        return 0

    staging_dir.mkdir(parents=True, exist_ok=True)

    # Always export a JSON archive for the user
    _export_json(items, staging_dir)

    if not is_privileged:
        logger.warning(
            "[health/android] Health data injection without root is not supported. "
            "Android HealthConnect requires SDK API calls that are only available "
            "to installed apps with explicit user consent. "
            "A JSON export of %d record(s) has been written to %s for reference. "
            "To import, use an app such as 'Health Sync' or 'Fit to Fit'.",
            len(items),
            staging_dir / "health_android_export.json",
        )
        return 0

    # Try Samsung Health injection (rooted Samsung devices)
    cfg = get_config()
    adb = ADBManager(cfg)
    return _inject_samsung_health(device_id, items, staging_dir, adb)


def _inject_samsung_health(
    device_id: str,
    items: list[HealthRecord],
    staging_dir: Path,
    adb: ADBManager,
) -> int:
    """Attempt to insert records into Samsung Health's SHealth7.db."""
    writable = [r for r in items if r.category in _SAMSUNG_WRITABLE]
    if not writable:
        logger.info(
            "[health/android] No Samsung-Health-writable records in the batch "
            "(writable types: %s)", sorted(_SAMSUNG_WRITABLE)
        )
        return 0

    local_db = staging_dir / "shealth_inject.db"

    # Pull existing DB
    _, _, rc = adb.shell_root(
        device_id,
        f"cp {_SHEALTH_DB_DEV} {_SHEALTH_SDCARD_PULL} && chmod 644 {_SHEALTH_SDCARD_PULL}",
        timeout=20,
    )
    if rc != 0:
        logger.warning(
            "[health/android] Samsung Health DB not found or not accessible on %s "
            "(Samsung Health may not be installed). rc=%d",
            device_id, rc,
        )
        return 0

    ok = adb.pull(device_id, _SHEALTH_SDCARD_PULL, local_db, timeout=60)
    adb.shell(device_id, f"rm -f {_SHEALTH_SDCARD_PULL}", timeout=10)

    if not ok or not local_db.exists():
        logger.error("[health/android] Failed to pull Samsung Health DB from %s", device_id)
        return 0

    inserted = _insert_into_shealth(local_db, writable)
    if inserted == 0:
        logger.warning("[health/android] Nothing inserted into Samsung Health DB")
        return 0

    # Push back
    ok = adb.push(device_id, local_db, _SHEALTH_SDCARD, timeout=60)
    if not ok:
        logger.error("[health/android] Failed to push modified Samsung Health DB to %s", device_id)
        return 0

    _, _, rc = adb.shell_root(
        device_id,
        f"cp {_SHEALTH_SDCARD} {_SHEALTH_DB_DEV} "
        f"&& chmod 660 {_SHEALTH_DB_DEV} "
        f"&& rm -f {_SHEALTH_SDCARD}",
        timeout=20,
    )
    if rc != 0:
        logger.error("[health/android] Failed to restore Samsung Health DB in-place. rc=%d", rc)
        return 0

    logger.info("[health/android] Injected %d record(s) into Samsung Health on %s", inserted, device_id)
    return inserted


def _insert_into_shealth(db_path: Path, records: list[HealthRecord]) -> int:
    inserted = 0
    try:
        with sqlite3.connect(str(db_path)) as conn:
            for rec in records:
                try:
                    _insert_record(conn, rec)
                    inserted += 1
                except Exception as exc:
                    logger.debug("[health/android] Skipping record %s/%s: %s", rec.category, rec.start, exc)
            conn.commit()
    except Exception as exc:
        logger.error("[health/android] DB open/write error: %s", exc)
    return inserted


def _insert_record(conn: sqlite3.Connection, rec: HealthRecord) -> None:
    start_ms = int(rec.start.timestamp() * 1000)
    end_ms   = int(rec.end.timestamp() * 1000) if rec.end else start_ms + 60_000
    row_uuid = str(uuid.uuid4())

    if rec.category == "steps":
        conn.execute(
            "INSERT INTO com_samsung_health_step_count "
            "(start_time, end_time, count, pkg_name, deviceuuid) "
            "VALUES (?, ?, ?, ?, ?)",
            (start_ms, end_ms, int(rec.value), "com.phonetransfer", row_uuid),
        )
    elif rec.category == "heart_rate":
        conn.execute(
            "INSERT INTO com_samsung_health_heart_rate "
            "(start_time, end_time, heart_rate, pkg_name, deviceuuid) "
            "VALUES (?, ?, ?, ?, ?)",
            (start_ms, end_ms, int(rec.value), "com.phonetransfer", row_uuid),
        )
    elif rec.category == "weight":
        conn.execute(
            "INSERT INTO com_samsung_health_weight "
            "(start_time, weight, pkg_name, deviceuuid) "
            "VALUES (?, ?, ?, ?)",
            (start_ms, rec.value, "com.phonetransfer", row_uuid),
        )
    else:
        raise ValueError(f"unsupported category: {rec.category}")


def _export_json(records: list[HealthRecord], staging_dir: Path) -> None:
    out = staging_dir / "health_android_export.json"
    try:
        data = []
        for r in records:
            data.append({
                "category":    r.category,
                "value":       r.value,
                "unit":        r.unit,
                "start":       r.start.isoformat(),
                "end":         r.end.isoformat() if r.end else None,
                "source_name": r.source_name,
                "notes":       r.notes,
            })
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("[health/android] Exported %d record(s) to %s", len(records), out)
    except Exception as exc:
        logger.warning("[health/android] JSON export failed: %s", exc)
