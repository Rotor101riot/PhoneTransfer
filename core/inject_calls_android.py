"""
inject_calls_android.py

Injects CallRecord entries into an Android device connected via USB/ADB.

Strategy
--------
1.  Content-provider insert via ``adb shell content insert --uri content://call_log/calls``
    Works on Android 4+.  Android 10+ restricts call log access to the
    default dialer app or carrier-privileged apps; inserts from the ADB shell
    user may be rejected with a SecurityException.  Those rejections are
    logged as warnings and do not abort the run.

2.  Rooted fallback — direct SQLite insert into calllog.db via ``sqlite3``.
    Only attempted when ``is_rooted=True`` and the content-provider insert
    returned a non-zero exit code.

Return value: count of call records successfully injected by either path.

Android call type constants
---------------------------
    1 = INCOMING_TYPE
    2 = OUTGOING_TYPE
    3 = MISSED_TYPE
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from core.adb_manager import ADBManager
from core.config_loader import get_config
from core.normalization_schema import CallRecord

logger = logging.getLogger(__name__)

_DEVICE_DIR   = "/sdcard/PhoneTransfer"
_CALLLOG_DB   = (
    "/data/data/com.android.providers.contacts/databases/calllog.db"
)
_URI_CALLS = "content://call_log/calls"


def _count_call_log(adb: ADBManager, serial: str) -> int | None:
    """Return the current call_log/calls row count, or None if unavailable."""
    try:
        stdout, _, rc = adb.shell(
            serial,
            f"content query --uri {_URI_CALLS} --projection _id",
            timeout=15,
        )
        if rc != 0:
            return None
        return sum(1 for line in stdout.splitlines() if line.strip().startswith("Row:"))
    except Exception:
        return None

_CALL_TYPE_MAP: dict[str, int] = {
    "incoming": 1,
    "outgoing": 2,
    "missed":   3,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _esc_shell(value: str) -> str:
    """Escape single quotes for use inside a shell single-quoted string."""
    return value.replace("'", "\\'")


def _to_unix_ms(dt: datetime) -> int:
    """Convert a datetime to Unix milliseconds (Android date column)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Content-provider path
# ---------------------------------------------------------------------------

def _insert_via_content_provider(
    adb: ADBManager,
    serial: str,
    record: CallRecord,
) -> bool:
    """
    Insert a single call record into the Android CallLog content provider.

    Returns True on success (rc == 0).
    """
    number   = _esc_shell(record.number)
    date_ms  = _to_unix_ms(record.timestamp)
    duration = record.duration_seconds
    call_type = _CALL_TYPE_MAP.get(record.call_type, 1)

    # Build command; name is optional
    cmd = (
        f"content insert --uri content://call_log/calls "
        f"--bind number:s:'{number}' "
        f"--bind date:i:{date_ms} "
        f"--bind duration:i:{duration} "
        f"--bind type:i:{call_type}"
    )
    if record.name:
        name = _esc_shell(record.name)
        cmd += f" --bind name:s:'{name}'"

    _, stderr, rc = adb.shell(serial, cmd, timeout=15)

    if rc != 0:
        if "SecurityException" in stderr or "permission" in stderr.lower():
            logger.warning(
                "inject_calls_android: call log content provider rejected "
                "insert (Android 10+ permission restriction). rc=%d: %s",
                rc, stderr.strip(),
            )
        else:
            logger.debug(
                "inject_calls_android: content insert rc=%d: %s",
                rc, stderr.strip(),
            )
        return False

    return True


# ---------------------------------------------------------------------------
# Rooted path — direct sqlite3 insert
# ---------------------------------------------------------------------------

def _build_sql_inserts(records: list[CallRecord]) -> str:
    """
    Build SQL INSERT statements for the ``calls`` table in calllog.db.

    Essential columns only: number, date, duration, type, name, new.
    """
    lines: list[str] = []
    for rec in records:
        number    = rec.number.replace("'", "''")
        date_ms   = _to_unix_ms(rec.timestamp)
        duration  = rec.duration_seconds
        call_type = _CALL_TYPE_MAP.get(rec.call_type, 1)
        name      = (rec.name or "").replace("'", "''")
        # 'new' = 1 means the call will appear as a new/unread entry
        lines.append(
            f"INSERT INTO calls (number, date, duration, type, name, new, "
            f"numbertype, numberlabel, countryiso, geocoded_location) VALUES "
            f"('{number}', {date_ms}, {duration}, {call_type}, '{name}', "
            f"0, 0, '', '', '');"
        )
    return "\n".join(lines)


def _insert_via_sqlite(
    adb: ADBManager,
    serial: str,
    records: list[CallRecord],
    staging_dir: Path,
) -> int:
    """
    Write a SQL script and execute it on the device via ``su -c sqlite3``.

    Returns the count of records written to the script (best-effort; individual
    row failures inside sqlite3 are not surfaced here).
    """
    if not records:
        return 0

    sql = _build_sql_inserts(records)
    if not sql.strip():
        return 0

    local_sql  = staging_dir / "calllog_insert.sql"
    remote_sql = f"{_DEVICE_DIR}/calllog_insert.sql"

    try:
        local_sql.write_text(sql, encoding="utf-8")
    except Exception as exc:
        logger.error(
            "inject_calls_android: failed to write SQL script: %s", exc
        )
        return 0

    if not adb.push(serial, local_sql, remote_sql):
        logger.error(
            "inject_calls_android: failed to push SQL script to device."
        )
        return 0

    _, stderr, rc = adb.shell_root(
        serial,
        f"sqlite3 {_CALLLOG_DB} < {remote_sql}",
        timeout=60,
    )
    if rc != 0:
        logger.error(
            "inject_calls_android: rooted sqlite3 insert failed rc=%d: %s",
            rc, stderr.strip(),
        )
        return 0

    logger.info(
        "inject_calls_android: rooted sqlite3 insert completed for %d record(s).",
        len(records),
    )
    return len(records)


# ---------------------------------------------------------------------------
# Public inject function
# ---------------------------------------------------------------------------

def inject(
    serial: str,
    items: list[CallRecord],
    staging_dir: Path,
    is_rooted: bool = False,
) -> int:
    """
    Inject call records into the Android device identified by *serial*.

    Parameters
    ----------
    serial:      ADB device serial.
    items:       CallRecord entries to inject.
    staging_dir: Local directory for temporary files (used by rooted path).
    is_rooted:   When True, direct sqlite3 insert is attempted as a fallback
                 for records the content provider rejects.

    Returns
    -------
    int: Number of call records successfully injected.
    """
    if not items:
        logger.info("inject_calls_android: no call records to inject — done.")
        return 0

    logger.info(
        "inject_calls_android: injecting %d call record(s) into device %s "
        "(rooted=%s)",
        len(items), serial, is_rooted,
    )

    try:
        cfg = get_config()
        adb = ADBManager(cfg)
    except Exception as exc:
        logger.error("inject_calls_android: failed to initialise ADB: %s", exc)
        return 0

    # Ensure device staging directory exists (needed for rooted SQL path)
    try:
        adb.shell(serial, f"mkdir -p {_DEVICE_DIR}")
    except Exception as exc:
        logger.warning(
            "inject_calls_android: mkdir -p %s error: %s", _DEVICE_DIR, exc
        )

    staging_dir.mkdir(parents=True, exist_ok=True)

    _pre_count = _count_call_log(adb, serial)
    success_count = 0
    cp_failed: list[CallRecord] = []

    # ── 1. Content-provider insert ────────────────────────────────────────────
    for i, record in enumerate(items):
        try:
            ok = _insert_via_content_provider(adb, serial, record)
            if ok:
                success_count += 1
            else:
                cp_failed.append(record)
        except Exception as exc:
            logger.warning(
                "inject_calls_android: unexpected error on record %d: %s",
                i, exc,
            )
            cp_failed.append(record)

    logger.info(
        "inject_calls_android: content provider succeeded for %d/%d record(s).",
        success_count, len(items),
    )

    # ── 2. Rooted fallback ────────────────────────────────────────────────────
    if cp_failed and is_rooted:
        logger.info(
            "inject_calls_android: attempting rooted sqlite3 fallback for "
            "%d record(s).",
            len(cp_failed),
        )
        try:
            rooted_count = _insert_via_sqlite(adb, serial, cp_failed, staging_dir)
            success_count += rooted_count
        except Exception as exc:
            logger.error(
                "inject_calls_android: rooted fallback raised unexpectedly: %s",
                exc,
            )
    elif cp_failed:
        logger.warning(
            "inject_calls_android: %d record(s) could not be injected via the "
            "content provider and rooted mode is disabled.  Enable rooted mode "
            "for a more complete call log transfer on Android 10+.",
            len(cp_failed),
        )

    logger.info(
        "inject_calls_android: total injected = %d/%d.",
        success_count, len(items),
    )

    # Post-write verification: confirm the call log row count grew as expected
    if success_count > 0:
        _post_count = _count_call_log(adb, serial)
        if _pre_count is not None and _post_count is not None:
            _delta = _post_count - _pre_count
            if _delta < success_count:
                logger.warning(
                    "inject_calls_android: post-write verification: "
                    "expected +%d call_log rows but only got +%d "
                    "(pre=%d post=%d) — OEM provider may have dropped rows",
                    success_count, _delta, _pre_count, _post_count,
                )
            else:
                logger.debug(
                    "inject_calls_android: post-write OK — call_log +%d rows",
                    _delta,
                )

    return success_count
