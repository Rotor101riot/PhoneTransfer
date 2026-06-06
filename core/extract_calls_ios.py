"""
extract_calls_ios.py

Extracts call history from an iOS device and returns a list of CallRecord
objects defined in normalization_schema.py.

Strategy
--------
1. Pull CallHistory.storedata via AFC2 (jailbroken) or iOSbackup (non-jailbroken).
2. CallHistory.storedata is a Core Data SQLite file.
   Query the ZCALLRECORD table.
3. Determine call direction via ZORIGINATED (1=outgoing, 0=incoming) and
   ZMISSED (1=missed).  Fall back to ZCALLTYPE if those columns are absent.
4. ZDATE is Apple epoch seconds (since 2001-01-01).

Never raises — all exceptions are caught, logged, and return partial/empty
results.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.normalization_schema import CallRecord

logger = logging.getLogger(__name__)

# Apple epoch offset
_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# Device / backup paths — modern (iOS 8+)
_DB_DEVICE_PATH    = "/var/mobile/Library/CallHistoryDB/CallHistory.storedata"
_DB_RELATIVE_PATH  = "Library/CallHistoryDB/CallHistory.storedata"
# iOS 8–13 stores call history under the Phone app domain; iOS 14+ moved it
# to HomeDomain.  Try both so we work across all versions.
_IOS_BACKUP_DOMAINS = [
    "AppDomain-com.apple.mobilephone",
    "HomeDomain",
]

# Legacy DB (iOS < 8)
_LEGACY_DEVICE_PATH   = "/var/mobile/Library/CallHistory/call_history.db"
_LEGACY_RELATIVE_PATH = "Library/CallHistory/call_history.db"
_LEGACY_BACKUP_DOMAINS = [
    "HomeDomain",
    "AppDomain-com.apple.mobilephone",
]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(udid: str, staging_dir: Path, is_jailbroken: bool = False) -> list[CallRecord]:
    """
    Extract call history from the iOS device identified by *udid*.

    Parameters
    ----------
    udid:           iOS device UDID.
    staging_dir:    Local directory used for temporary file copies.
    is_jailbroken:  Whether the device has AFC2 available.

    Returns
    -------
    list[CallRecord]   Possibly empty on total failure.
    """
    try:
        return _extract_impl(udid, staging_dir, is_jailbroken)
    except Exception as exc:
        logger.exception("extract_calls_ios: top-level failure for %s: %s", udid, exc)
        return []


def _extract_impl(udid: str, staging_dir: Path, is_jailbroken: bool) -> list[CallRecord]:
    work_dir = staging_dir / "calls_ios"
    work_dir.mkdir(parents=True, exist_ok=True)

    db_path = _pull_callhistory_db(udid, work_dir, is_jailbroken)
    if db_path is not None:
        records = _parse_callhistory_db(db_path)
        logger.info("calls_ios: extracted %d call records for %s", len(records), udid)
        return records

    # Try legacy call_history.db (iOS < 8)
    legacy_path = _pull_legacy_callhistory_db(udid, work_dir, is_jailbroken)
    if legacy_path is not None:
        records = _parse_legacy_callhistory_db(legacy_path)
        logger.info(
            "calls_ios: extracted %d call records (legacy DB) for %s", len(records), udid
        )
        return records

    logger.warning("calls_ios: could not obtain call history DB for %s", udid)
    return []


# ---------------------------------------------------------------------------
# Pull CallHistory.storedata
# ---------------------------------------------------------------------------

def _pull_legacy_callhistory_db(udid: str, work_dir: Path, is_jailbroken: bool) -> Path | None:
    """Pull the legacy call_history.db used on iOS < 8."""
    local_db = work_dir / "call_history_legacy.db"

    if is_jailbroken:
        try:
            from core.device_connection_cache import get_broker
            from core.afc2_connector import AFC2Connector

            broker = get_broker(udid)
            with AFC2Connector(broker) as afc2:
                ok = afc2.pull_file(_LEGACY_DEVICE_PATH, local_db)
            if ok and local_db.exists():
                logger.debug("calls_ios: pulled legacy call_history.db via AFC2")
                return local_db
        except Exception as exc:
            logger.debug("calls_ios: legacy AFC2 pull failed: %s", exc)

    return _pull_via_iosbackup_multi(udid, _LEGACY_RELATIVE_PATH, _LEGACY_BACKUP_DOMAINS, local_db)


def _parse_legacy_callhistory_db(db_path: Path) -> list[CallRecord]:
    """
    Parse the iOS < 8 call_history.db (plain SQLite, not Core Data).

    Schema: ZCALLRECORD table with columns:
      address      — phone number
      name         — contact name (may be null)
      date         — Apple epoch seconds
      duration     — seconds (integer)
      flags        — bitmask: bit0=incoming(0)/outgoing(1), bit2=missed
    """
    records: list[CallRecord] = []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                cur = conn.execute(
                    "SELECT address, name, date, duration, flags "
                    "FROM call ORDER BY date ASC"
                )
            except sqlite3.OperationalError:
                try:
                    cur = conn.execute(
                        "SELECT address, name, date, duration, flags "
                        "FROM ZCALLRECORD ORDER BY date ASC"
                    )
                except sqlite3.OperationalError as exc:
                    logger.error("calls_ios: legacy DB table not found: %s", exc)
                    return records

            for row in cur.fetchall():
                try:
                    flags = row["flags"] or 0
                    if flags & 4:
                        call_type = "missed"
                    elif flags & 1:
                        call_type = "outgoing"
                    else:
                        call_type = "incoming"

                    records.append(CallRecord(
                        number=row["address"] or "unknown",
                        timestamp=_apple_ts_to_datetime(row["date"]),
                        duration_seconds=int(row["duration"] or 0),
                        call_type=call_type,  # type: ignore[arg-type]
                        name=row["name"] or None,
                    ))
                except Exception as exc:
                    logger.debug("calls_ios: skipping legacy call row: %s", exc)
    except Exception as exc:
        logger.exception("calls_ios: failed to parse legacy call_history.db: %s", exc)
    return records


def _pull_callhistory_db(udid: str, work_dir: Path, is_jailbroken: bool) -> Path | None:
    local_db = work_dir / "CallHistory.storedata"

    if is_jailbroken:
        try:
            from core.device_connection_cache import get_broker
            from core.afc2_connector import AFC2Connector

            broker = get_broker(udid)
            with AFC2Connector(broker) as afc2:
                ok = afc2.pull_file(_DB_DEVICE_PATH, local_db)
            if ok and local_db.exists():
                logger.debug("calls_ios: pulled CallHistory.storedata via AFC2")
                return local_db
        except PermissionError:
            logger.warning("calls_ios: AFC2 not available despite is_jailbroken=True")
        except Exception as exc:
            logger.warning("calls_ios: AFC2 pull failed: %s", exc)

    return _pull_via_iosbackup_multi(udid, _DB_RELATIVE_PATH, _IOS_BACKUP_DOMAINS, local_db)


def _pull_via_iosbackup_multi(
    udid: str, relative_path: str, domains: list[str], dest: Path
) -> Path | None:
    """Try pulling *relative_path* from each domain in *domains* until one succeeds."""
    try:
        from core.device_connection_cache import get_iosbackup
        dest.parent.mkdir(parents=True, exist_ok=True)
        backup = get_iosbackup(udid)
    except Exception as exc:
        logger.warning("calls_ios: could not open iOSbackup for %s: %s", udid, exc)
        return None

    # iOSbackup searches all domains by relativePath — no domain filter needed.
    # We keep the domains list for logging context only.
    try:
        from core.iosbackup_helpers import fix_truncated_sqlite
        if dest.exists():
            dest.unlink()
        info = backup.getFileDecryptedCopy(
            relativePath=relative_path,
            targetName=dest.name,
            targetFolder=str(dest.parent),
        )
        if info and dest.exists():
            fix_truncated_sqlite(dest, backup, relative_path)
            logger.debug("calls_ios: pulled %s via iOSbackup", relative_path)
            return dest
    except Exception as exc:
        logger.debug("calls_ios: iOSbackup pull failed for %s: %s", relative_path, exc)

    logger.warning(
        "calls_ios: iOSbackup pull failed for %s (tried domains: %s). "
        "Call history is excluded from unencrypted backups by iOS — "
        "enable backup encryption and retry.",
        relative_path, domains,
    )
    return None


# ---------------------------------------------------------------------------
# Timestamp conversion
# ---------------------------------------------------------------------------

def _apple_ts_to_datetime(ts: float | int | None) -> datetime:
    """Convert Apple epoch seconds (since 2001-01-01) to UTC datetime."""
    if ts is None or ts == 0:
        return _APPLE_EPOCH
    try:
        return _APPLE_EPOCH + timedelta(seconds=float(ts))
    except (OverflowError, OSError, ValueError):
        return _APPLE_EPOCH


# ---------------------------------------------------------------------------
# Parse CallHistory.storedata (Core Data SQLite)
# ---------------------------------------------------------------------------

def _parse_callhistory_db(db_path: Path) -> list[CallRecord]:
    """
    Read ZCALLRECORD table from the Core Data SQLite file and build
    CallRecord objects.

    Column reference (may vary by iOS version):
      ZADDRESS      — phone number / caller ID (text)
      ZNAME         — resolved display name (text, may be NULL)
      ZDATE         — Apple epoch seconds (real/float)
      ZDURATION     — call duration in seconds (real/float)
      ZORIGINATED   — 1 if outgoing, 0 if incoming
      ZMISSED       — 1 if missed
      ZCALLTYPE     — legacy type flag (0=incoming,1=outgoing on some iOS)
    """
    records: list[CallRecord] = []

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row

            # Discover available columns to handle schema differences
            try:
                col_info = conn.execute("PRAGMA table_info(ZCALLRECORD)").fetchall()
                available_cols = {row[1].upper() for row in col_info}
            except Exception as exc:
                logger.error("calls_ios: PRAGMA table_info failed: %s", exc)
                return []

            if not available_cols:
                logger.error("calls_ios: ZCALLRECORD table not found in %s", db_path)
                return []

            # Build SELECT list dynamically based on available columns
            select_cols = ["ZADDRESS", "ZDATE", "ZDURATION"]
            optional = ["ZNAME", "ZORIGINATED", "ZMISSED", "ZCALLTYPE", "ZSERVICE_PROVIDER"]
            for col in optional:
                if col in available_cols:
                    select_cols.append(col)

            query = f"SELECT {', '.join(select_cols)} FROM ZCALLRECORD ORDER BY ZDATE ASC"

            try:
                cur = conn.execute(query)
            except sqlite3.OperationalError as exc:
                logger.error("calls_ios: ZCALLRECORD query failed: %s", exc)
                return []

            has_originated       = "ZORIGINATED"      in available_cols
            has_missed           = "ZMISSED"           in available_cols
            has_calltype         = "ZCALLTYPE"         in available_cols
            has_name             = "ZNAME"             in available_cols
            has_service_provider = "ZSERVICE_PROVIDER" in available_cols

            for row in cur.fetchall():
                try:
                    rec = _row_to_call_record(
                        row,
                        has_originated=has_originated,
                        has_missed=has_missed,
                        has_calltype=has_calltype,
                        has_name=has_name,
                        has_service_provider=has_service_provider,
                    )
                    records.append(rec)
                except Exception as exc:
                    logger.debug("calls_ios: skipping call row: %s", exc)

    except Exception as exc:
        logger.exception("calls_ios: failed to parse CallHistory.storedata: %s", exc)

    return records


def _row_to_call_record(
    row: sqlite3.Row,
    *,
    has_originated: bool,
    has_missed: bool,
    has_calltype: bool,
    has_name: bool,
    has_service_provider: bool,
) -> CallRecord:
    number = (row["ZADDRESS"] or "unknown").strip()
    ts = _apple_ts_to_datetime(row["ZDATE"])
    duration = int(float(row["ZDURATION"] or 0))
    name = row["ZNAME"].strip() if has_name and row["ZNAME"] else None

    # Annotate FaceTime calls in the display name
    if has_service_provider and row["ZSERVICE_PROVIDER"]:
        provider = str(row["ZSERVICE_PROVIDER"]).strip()
        if "facetime" in provider.lower():
            label = "FaceTime"
            name = f"{name} ({label})" if name else f"{label}: {number}"

    # Determine call type
    call_type: str
    if has_missed and row["ZMISSED"]:
        call_type = "missed"
    elif has_originated:
        call_type = "outgoing" if row["ZORIGINATED"] else "incoming"
    elif has_calltype:
        # Heuristic: ZCALLTYPE == 1 often means outgoing in older iOS versions
        call_type = "outgoing" if row["ZCALLTYPE"] == 1 else "incoming"
    else:
        call_type = "incoming"  # safe default

    return CallRecord(
        number=number,
        timestamp=ts,
        duration_seconds=duration,
        call_type=call_type,  # type: ignore[arg-type]
        name=name,
    )
