"""
extract_calls_android.py

Extracts call log records from an Android device connected via ADB.

Two extraction paths:
- Non-rooted: uses the call_log content provider via
  `adb shell content query --uri content://call_log/calls`.
- Rooted: copies calllog.db directly from the telephony provider data
  directory to /sdcard/, pulls it locally, and parses via sqlite3.
  Falls back to the content provider path on any failure.

Call type constants (android.provider.CallLog.Calls):
    1 = INCOMING_TYPE
    2 = OUTGOING_TYPE
    3 = MISSED_TYPE
    4 = VOICEMAIL_TYPE  — excluded

Returns a list of CallRecord objects as defined in normalization_schema.py.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.adb_manager import ADBManager
from core.config_loader import get_config
from core.normalization_schema import CallRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_URI_CALLS = "content://call_log/calls"

_TYPE_INCOMING = 1
_TYPE_OUTGOING = 2
_TYPE_MISSED = 3
_TYPE_VOICEMAIL = 4  # excluded from output

_CALL_TYPE_MAP: dict[int, str] = {
    _TYPE_INCOMING: "incoming",
    _TYPE_OUTGOING: "outgoing",
    _TYPE_MISSED: "missed",
}

# Remote DB location (on rooted devices)
_REMOTE_DB = (
    "/data/data/com.android.providers.telephony/databases/calllog.db"
)
_REMOTE_TMP = "/sdcard/calllog_tmp.db"
_LOCAL_DB_NAME = "calllog.db"

# Staging sub-directory
_SUBDIR = "calls_android"


# ---------------------------------------------------------------------------
# Content row parser
# ---------------------------------------------------------------------------

def _parse_content_rows(output: str) -> list[dict[str, str]]:
    """
    Parse the stdout of `adb shell content query` into a list of dicts.

    Handles values that contain commas by splitting only at ", key=" boundaries.
    """
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("Row:"):
            continue
        _, _, rest = line.partition(" ")   # drop "Row:"
        _, _, rest = rest.partition(" ")   # drop row index
        rest = rest.strip()
        if not rest:
            continue
        pairs = re.split(r',\s+(?=\w+=)', rest)
        row: dict[str, str] = {}
        for pair in pairs:
            k, _, v = pair.partition("=")
            row[k.strip()] = v.strip()
        rows.append(row)
    return rows


def _ms_to_dt(ms_str: str) -> datetime:
    """Convert a Unix milliseconds string to a UTC-aware datetime."""
    try:
        ts = int(ms_str) / 1000.0
    except (ValueError, TypeError):
        ts = 0.0
    return datetime.utcfromtimestamp(ts).replace(tzinfo=timezone.utc)


def _safe_int(value: str | None, default: int = 0) -> int:
    """Convert a string to int, returning *default* on failure."""
    try:
        return int(value or default)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(
    serial: str,
    staging_dir: Path,
    is_rooted: bool = False,
) -> list[CallRecord]:
    """
    Extract all call log records from the Android device.

    Parameters
    ----------
    serial:      ADB device serial string.
    staging_dir: Root staging directory for this transfer session.
    is_rooted:   If True, attempt direct DB pull first.

    Returns
    -------
    List of CallRecord objects; empty list on any fatal error.
    """
    try:
        sub = staging_dir / _SUBDIR
        sub.mkdir(parents=True, exist_ok=True)

        adb = ADBManager(get_config())

        if is_rooted:
            records = _extract_rooted(serial, sub, adb)
            if records is not None:
                logger.info(
                    "[calls/android] Rooted path: extracted %d records",
                    len(records),
                )
                return records
            logger.warning(
                "[calls/android] Rooted path failed, falling back to "
                "content provider"
            )

        records = _extract_content_provider(serial, adb)
        logger.info(
            "[calls/android] Content provider path: extracted %d records",
            len(records),
        )
        return records

    except Exception:
        logger.exception("[calls/android] Unhandled error during extraction")
        return []


# ---------------------------------------------------------------------------
# Non-rooted path — content provider
# ---------------------------------------------------------------------------

def _extract_content_provider(serial: str, adb: ADBManager) -> list[CallRecord]:
    """Query the call log via Android content providers (no root required)."""
    stdout, stderr, rc = adb.shell(
        serial,
        (
            "content query "
            f"--uri {_URI_CALLS} "
            "--projection _id,number,date,duration,type,name"
        ),
        timeout=60,
    )
    if rc != 0:
        logger.warning(
            "[calls/android] call_log query failed (rc=%d): %s", rc, stderr
        )
        return []

    rows = _parse_content_rows(stdout)
    return _rows_to_records(rows)


def _rows_to_records(rows: list[dict[str, str]]) -> list[CallRecord]:
    """Convert parsed content rows into CallRecord objects."""
    records: list[CallRecord] = []
    for row in rows:
        call_type_int = _safe_int(row.get("type", "0"))
        if call_type_int == _TYPE_VOICEMAIL:
            continue  # voicemail entries are not standard call records
        if call_type_int not in _CALL_TYPE_MAP:
            logger.debug(
                "[calls/android] Unknown call type %d, skipping", call_type_int
            )
            continue

        number = row.get("number", "").strip() or "unknown"
        name_raw = row.get("name", "").strip()
        name: str | None = name_raw if name_raw and name_raw != "null" else None
        duration = _safe_int(row.get("duration", "0"))
        timestamp = _ms_to_dt(row.get("date", "0"))
        call_type = _CALL_TYPE_MAP[call_type_int]  # type: ignore[assignment]

        records.append(
            CallRecord(
                number=number,
                timestamp=timestamp,
                duration_seconds=duration,
                call_type=call_type,  # type: ignore[arg-type]
                name=name,
            )
        )

    return records


# ---------------------------------------------------------------------------
# Rooted path — direct SQLite access
# ---------------------------------------------------------------------------

def _extract_rooted(
    serial: str,
    sub: Path,
    adb: ADBManager,
) -> list[CallRecord] | None:
    """
    Copy calllog.db off the device, pull to staging, parse locally.
    Returns None on any failure so the caller can fall back.
    """
    local_db = sub / _LOCAL_DB_NAME

    _, _, rc = adb.shell_root(
        serial,
        f"cp {_REMOTE_DB} {_REMOTE_TMP}",
        timeout=30,
    )
    if rc != 0:
        logger.warning("[calls/android] su cp failed (rc=%d)", rc)
        return None

    adb.shell_root(serial, f"chmod 644 {_REMOTE_TMP}", timeout=10)
    pulled = adb.pull_verified(serial, _REMOTE_TMP, local_db, timeout=60)
    adb.shell(serial, f"rm -f {_REMOTE_TMP}", timeout=10)

    if not pulled or not local_db.exists():
        logger.warning("[calls/android] adb pull of calllog.db failed")
        return None

    try:
        return _parse_sqlite_calls(local_db)
    except Exception:
        logger.exception("[calls/android] SQLite parse error")
        return None


def _parse_sqlite_calls(db_path: Path) -> list[CallRecord]:
    """Parse calllog.db directly using sqlite3."""
    records: list[CallRecord] = []

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(
                "SELECT number, date, duration, type, name FROM calls"
            )
            for row in cursor:
                call_type_int = row["type"] or 0
                if call_type_int == _TYPE_VOICEMAIL:
                    continue
                if call_type_int not in _CALL_TYPE_MAP:
                    logger.debug(
                        "[calls/android] Unknown call type %d in DB, skipping",
                        call_type_int,
                    )
                    continue

                number = (row["number"] or "unknown").strip()
                name_raw = (row["name"] or "").strip()
                name: str | None = (
                    name_raw if name_raw and name_raw != "null" else None
                )
                duration = int(row["duration"] or 0)
                timestamp = _ms_to_dt(str(row["date"] or 0))
                call_type = _CALL_TYPE_MAP[call_type_int]

                records.append(
                    CallRecord(
                        number=number,
                        timestamp=timestamp,
                        duration_seconds=duration,
                        call_type=call_type,  # type: ignore[arg-type]
                        name=name,
                    )
                )
        except sqlite3.OperationalError:
            logger.warning(
                "[calls/android] calls table unavailable in calllog.db"
            )

    return records
