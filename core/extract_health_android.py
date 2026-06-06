"""
extract_health_android.py

Extracts health and fitness data from an Android device.

Supported sources (tried in order):
  1. Samsung Health (SHealth7.db) — most common on Samsung devices (root)
  2. Google HealthConnect database (Android 14+ native) (root)
  3. Google Fit fitness database (root, deprecated but present on older phones)

All paths require root access because health databases are stored in private
app data directories.  Non-rooted devices return [] with a guidance message.

Returns a list of HealthRecord objects as defined in normalization_schema.py.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.adb_manager import ADBManager
from core.config_loader import get_config
from core.normalization_schema import HealthRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Remote paths
# ---------------------------------------------------------------------------

# Samsung Health (Samsung devices)
_SHEALTH_PKG      = "com.sec.android.app.shealth"
_SHEALTH_DB_PATH  = f"/data/data/{_SHEALTH_PKG}/databases/SHealth7.db"
_SHEALTH_SDCARD   = "/sdcard/PT_shealth_tmp.db"

# Google HealthConnect (Android 14+)
_HC_PKG           = "com.google.android.platform.health"
_HC_DB_PATH       = f"/data/data/{_HC_PKG}/databases/health-data.db"
_HC_SDCARD        = "/sdcard/PT_hc_tmp.db"

# Google Fit (deprecated, Android < 14)
_GFIT_PKG         = "com.google.android.gms"
_GFIT_DB_PATH     = f"/data/data/{_GFIT_PKG}/databases/fitness_sessions_store"
_GFIT_SDCARD      = "/sdcard/PT_gfit_tmp.db"

# Apple/Unix epoch offset (ms → seconds → UTC datetime)
_UNIX_MS   = 1_000        # divide to get seconds


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(
    serial: str,
    staging_dir: Path,
    is_rooted: bool = False,
) -> list[HealthRecord]:
    """
    Extract health records from an Android device.

    Non-rooted: returns [] with a user-facing guidance message.
    Rooted: tries Samsung Health, HealthConnect, and Google Fit in order,
            returning whichever source yields results first.
    """
    if not is_rooted:
        logger.warning(
            "[health/android] Health data extraction requires root access. "
            "Android health databases (Samsung Health, HealthConnect) reside in "
            "protected app directories that are inaccessible without root. "
            "Grant root access in your device's root manager and try again."
        )
        return []

    try:
        return _extract_impl(serial, staging_dir)
    except Exception:
        logger.exception("[health/android] Unhandled error during extraction")
        return []


def _extract_impl(serial: str, staging_dir: Path) -> list[HealthRecord]:
    sub = staging_dir / "health_android"
    sub.mkdir(parents=True, exist_ok=True)

    adb = ADBManager(get_config())

    # Try Samsung Health first (most common on Samsung devices)
    records = _try_samsung_health(serial, sub, adb)
    if records:
        logger.info("[health/android] Extracted %d records from Samsung Health", len(records))
        return records

    # Try HealthConnect (Android 14+)
    records = _try_healthconnect(serial, sub, adb)
    if records:
        logger.info("[health/android] Extracted %d records from HealthConnect", len(records))
        return records

    # Try Google Fit (deprecated, older devices)
    records = _try_google_fit(serial, sub, adb)
    if records:
        logger.info("[health/android] Extracted %d records from Google Fit", len(records))
        return records

    logger.warning(
        "[health/android] No supported health database found on device %s. "
        "Supported sources: Samsung Health, HealthConnect, Google Fit.",
        serial,
    )
    return []


# ---------------------------------------------------------------------------
# Samsung Health
# ---------------------------------------------------------------------------

def _try_samsung_health(serial: str, sub: Path, adb: ADBManager) -> list[HealthRecord]:
    local_db = sub / "shealth.db"
    if not _pull_private_db(serial, _SHEALTH_DB_PATH, _SHEALTH_SDCARD, local_db, adb):
        return []

    records: list[HealthRecord] = []
    try:
        with sqlite3.connect(str(local_db)) as conn:
            conn.row_factory = sqlite3.Row
            records.extend(_parse_shealth_steps(conn))
            records.extend(_parse_shealth_heart_rate(conn))
            records.extend(_parse_shealth_sleep(conn))
            records.extend(_parse_shealth_exercise(conn))
            records.extend(_parse_shealth_weight(conn))
    except Exception as exc:
        logger.warning("[health/android] Samsung Health DB parse error: %s", exc)

    return records


def _parse_shealth_steps(conn: sqlite3.Connection) -> list[HealthRecord]:
    records = []
    try:
        cur = conn.execute(
            "SELECT start_time, end_time, count "
            "FROM com_samsung_health_step_count "
            "ORDER BY start_time ASC"
        )
        for row in cur.fetchall():
            records.append(HealthRecord(
                category="steps",
                value=float(row["count"] or 0),
                unit="count",
                start=_ms_to_dt(row["start_time"]),
                end=_ms_to_dt(row["end_time"]),
                source_name="Samsung Health",
            ))
    except sqlite3.OperationalError:
        pass
    return records


def _parse_shealth_heart_rate(conn: sqlite3.Connection) -> list[HealthRecord]:
    records = []
    try:
        cur = conn.execute(
            "SELECT start_time, end_time, heart_rate "
            "FROM com_samsung_health_heart_rate "
            "ORDER BY start_time ASC"
        )
        for row in cur.fetchall():
            if row["heart_rate"]:
                records.append(HealthRecord(
                    category="heart_rate",
                    value=float(row["heart_rate"]),
                    unit="bpm",
                    start=_ms_to_dt(row["start_time"]),
                    end=_ms_to_dt(row["end_time"]),
                    source_name="Samsung Health",
                ))
    except sqlite3.OperationalError:
        pass
    return records


def _parse_shealth_sleep(conn: sqlite3.Connection) -> list[HealthRecord]:
    records = []
    # Samsung Health 6.x uses com_samsung_health_sleep
    for table in ("com_samsung_health_sleep", "com_samsung_health_sleep_stage"):
        try:
            cur = conn.execute(
                f"SELECT start_time, end_time FROM {table} ORDER BY start_time ASC"
            )
            for row in cur.fetchall():
                start = _ms_to_dt(row["start_time"])
                end   = _ms_to_dt(row["end_time"])
                dur_min = (end - start).total_seconds() / 60 if end else 0
                records.append(HealthRecord(
                    category="sleep",
                    value=dur_min,
                    unit="min",
                    start=start,
                    end=end,
                    source_name="Samsung Health",
                ))
            if records:
                break
        except sqlite3.OperationalError:
            continue
    return records


def _parse_shealth_exercise(conn: sqlite3.Connection) -> list[HealthRecord]:
    records = []
    try:
        cur = conn.execute(
            "SELECT start_time, end_time, exercise_type, calorie "
            "FROM com_samsung_health_exercise "
            "ORDER BY start_time ASC"
        )
        for row in cur.fetchall():
            records.append(HealthRecord(
                category="workout",
                value=float(row["calorie"] or 0),
                unit="kcal",
                start=_ms_to_dt(row["start_time"]),
                end=_ms_to_dt(row["end_time"]),
                source_name="Samsung Health",
                notes=f"exercise_type={row['exercise_type']}",
            ))
    except sqlite3.OperationalError:
        pass
    return records


def _parse_shealth_weight(conn: sqlite3.Connection) -> list[HealthRecord]:
    records = []
    try:
        cur = conn.execute(
            "SELECT start_time, weight FROM com_samsung_health_weight "
            "ORDER BY start_time ASC"
        )
        for row in cur.fetchall():
            if row["weight"]:
                records.append(HealthRecord(
                    category="weight",
                    value=float(row["weight"]),
                    unit="kg",
                    start=_ms_to_dt(row["start_time"]),
                    source_name="Samsung Health",
                ))
    except sqlite3.OperationalError:
        pass
    return records


# ---------------------------------------------------------------------------
# Google HealthConnect
# ---------------------------------------------------------------------------

def _try_healthconnect(serial: str, sub: Path, adb: ADBManager) -> list[HealthRecord]:
    local_db = sub / "healthconnect.db"
    if not _pull_private_db(serial, _HC_DB_PATH, _HC_SDCARD, local_db, adb):
        return []

    records: list[HealthRecord] = []
    try:
        with sqlite3.connect(str(local_db)) as conn:
            conn.row_factory = sqlite3.Row
            records.extend(_parse_hc_db(conn))
    except Exception as exc:
        logger.debug("[health/android] HealthConnect DB parse error: %s", exc)

    return records


def _parse_hc_db(conn: sqlite3.Connection) -> list[HealthRecord]:
    """
    Parse the HealthConnect database.  The schema is internal and may change;
    we attempt to read from known table/column patterns and skip tables that
    don't exist.
    """
    records = []

    # Try reading steps (StepsRecord)
    for table, val_col in [
        ("StepsRecord", "count"),
        ("steps_record", "count"),
        ("step_count_record", "count"),
    ]:
        try:
            cur = conn.execute(
                f"SELECT start_time_epoch_ms, end_time_epoch_ms, {val_col} "
                f"FROM {table} ORDER BY start_time_epoch_ms ASC"
            )
            for row in cur.fetchall():
                records.append(HealthRecord(
                    category="steps",
                    value=float(row[val_col] or 0),
                    unit="count",
                    start=_ms_to_dt(row["start_time_epoch_ms"]),
                    end=_ms_to_dt(row["end_time_epoch_ms"]),
                    source_name="HealthConnect",
                ))
            break
        except sqlite3.OperationalError:
            continue

    # Try heart rate
    for table, val_col in [
        ("HeartRateRecord", "bpm"),
        ("heart_rate_record", "bpm"),
    ]:
        try:
            cur = conn.execute(
                f"SELECT time_epoch_ms, {val_col} FROM {table} ORDER BY time_epoch_ms ASC"
            )
            for row in cur.fetchall():
                records.append(HealthRecord(
                    category="heart_rate",
                    value=float(row[val_col] or 0),
                    unit="bpm",
                    start=_ms_to_dt(row["time_epoch_ms"]),
                    source_name="HealthConnect",
                ))
            break
        except sqlite3.OperationalError:
            continue

    return records


# ---------------------------------------------------------------------------
# Google Fit (deprecated)
# ---------------------------------------------------------------------------

def _try_google_fit(serial: str, sub: Path, adb: ADBManager) -> list[HealthRecord]:
    local_db = sub / "gfit.db"
    if not _pull_private_db(serial, _GFIT_DB_PATH, _GFIT_SDCARD, local_db, adb):
        return []

    records: list[HealthRecord] = []
    try:
        with sqlite3.connect(str(local_db)) as conn:
            conn.row_factory = sqlite3.Row
            # Google Fit sessions table
            for table in ("fitness_sessions", "sessions"):
                try:
                    cur = conn.execute(
                        f"SELECT start_time_ms, end_time_ms, activity_type "
                        f"FROM {table} ORDER BY start_time_ms ASC"
                    )
                    for row in cur.fetchall():
                        start = _ms_to_dt(row["start_time_ms"])
                        end   = _ms_to_dt(row["end_time_ms"])
                        dur_min = (end - start).total_seconds() / 60 if end else 0
                        records.append(HealthRecord(
                            category="workout",
                            value=dur_min,
                            unit="min",
                            start=start,
                            end=end,
                            source_name="Google Fit",
                            notes=f"activity_type={row['activity_type']}",
                        ))
                    break
                except sqlite3.OperationalError:
                    continue
    except Exception as exc:
        logger.debug("[health/android] Google Fit DB parse error: %s", exc)

    return records


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _pull_private_db(
    serial: str,
    device_path: str,
    sdcard_tmp: str,
    local_path: Path,
    adb: ADBManager,
) -> bool:
    """
    Root-copy a private database to /sdcard/, pull it, then clean up.
    Returns True if local_path exists and is non-empty.
    """
    _, _, rc = adb.shell_root(
        serial, f"cp {device_path} {sdcard_tmp} && chmod 644 {sdcard_tmp}", timeout=20
    )
    if rc != 0:
        logger.debug("[health/android] Could not root-copy %s (rc=%d)", device_path, rc)
        return False

    ok = adb.pull(serial, sdcard_tmp, local_path, timeout=60)
    adb.shell(serial, f"rm -f {sdcard_tmp}", timeout=10)

    if not ok or not local_path.exists() or local_path.stat().st_size == 0:
        logger.debug("[health/android] Pull failed for %s", device_path)
        return False

    return True


def _ms_to_dt(ts_ms: int | float | None) -> datetime:
    """Convert a Unix millisecond timestamp to a UTC datetime."""
    if ts_ms is None or ts_ms == 0:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        return datetime.utcfromtimestamp(float(ts_ms) / _UNIX_MS).replace(tzinfo=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
