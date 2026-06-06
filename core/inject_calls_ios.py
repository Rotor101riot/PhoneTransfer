"""
inject_calls_ios.py

Inject call-log entries into ``HomeDomain:Library/CallHistoryDB/CallHistory.storedata``
via the active backup injector.

Core Data specifics (from G:/test/modify_calls.py):
  - ``ZCALLRECORD`` has its own Z_ENT=2 in Z_PRIMARYKEY.  We must bump
    ``Z_PRIMARYKEY.Z_MAX`` for the CallRecord entity after INSERTing, or
    iOS may later pick the same Z_PK and corrupt the store.
  - ``ZDATE`` is Apple-epoch SECONDS as REAL (not nanoseconds).
  - ``ZCALLTYPE``: 1 = Phone, 8 = FaceTime Video, 16 = FaceTime Audio.
    The normalized :class:`CallRecord` doesn't distinguish FaceTime, so we
    map everything to Phone (1).  Callers that need FaceTime can drop an
    ``ios_call_type`` attribute on the record before invoking this module.
  - ``ZORIGINATED``: 1 = outgoing, 0 = incoming.  Missed calls carry
    ``ZANSWERED=0``.  Normalized ``call_type`` is
    ``"incoming" | "outgoing" | "missed"``.
  - ``ZUNIQUE_ID`` is UNIQUE — fresh UUID per row.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.ios_backup_injector import IOSBackupInjector, get_current_injector
from core.normalization_schema import CallRecord

logger = logging.getLogger(__name__)


CALLS_DB_DOMAIN = "HomeDomain"
CALLS_DB_RELPATH = "Library/CallHistoryDB/CallHistory.storedata"

APPLE_EPOCH_OFFSET = 978307200
CALLRECORD_ENT = 2

CALLTYPE_PHONE = 1
CALLTYPE_FACETIME_VIDEO = 8
CALLTYPE_FACETIME_AUDIO = 16
HANDLE_TYPE_PHONE = 2


def inject(
    device_id: str,
    items: list[CallRecord],
    staging_dir: Path,
    is_privileged: bool,
) -> int:
    if not items:
        logger.info("inject_calls_ios: no call records for %s", device_id)
        return 0

    injector = get_current_injector()
    if injector is None:
        logger.warning(
            "inject_calls_ios: no backup injector active — falling back to "
            "JSON/TXT export under %s", staging_dir,
        )
        _export_fallback(items, staging_dir)
        return 0

    return _inject_via_backup(injector, items)


def _inject_via_backup(
    injector: IOSBackupInjector, records: list[CallRecord]
) -> int:
    db_path = injector.stage_db(CALLS_DB_DOMAIN, CALLS_DB_RELPATH)

    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=FULL")

        cur_max_row = con.execute(
            "SELECT Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME=?", ("CallRecord",)
        ).fetchone()
        if cur_max_row is None:
            raise RuntimeError(
                "CallHistory.storedata: no Z_PRIMARYKEY entry for 'CallRecord'"
            )
        cur_max = cur_max_row[0]

        inserted = 0
        with con:
            for i, rec in enumerate(records):
                z_pk = cur_max + 1 + i
                z_date = _apple_seconds(rec.timestamp)
                originated = 1 if rec.call_type == "outgoing" else 0
                answered = 0 if rec.call_type == "missed" else 1
                call_type = getattr(rec, "ios_call_type", CALLTYPE_PHONE)
                service = ("com.apple.Telephony"
                           if call_type == CALLTYPE_PHONE
                           else "com.apple.FaceTime")
                con.execute(
                    """
                    INSERT INTO ZCALLRECORD (
                        Z_PK, Z_ENT, Z_OPT,
                        ZANSWERED, ZCALL_CATEGORY, ZCALLTYPE,
                        ZDISCONNECTED_CAUSE, ZHANDLE_TYPE, ZJUNKCONFIDENCE,
                        ZNUMBER_AVAILABILITY, ZORIGINATED, ZREAD,
                        ZVERIFICATIONSTATUS,
                        ZDATE, ZDURATION,
                        ZADDRESS, ZISO_COUNTRY_CODE, ZLOCATION, ZNAME,
                        ZSERVICE_PROVIDER, ZUNIQUE_ID,
                        ZAUTOANSWEREDREASON, ZWASEMERGENCYCALL,
                        ZUSEDEMERGENCYVIDEOSTREAMING,
                        ZCALLDIRECTORYIDENTITYTYPE,
                        ZSCREENSHARINGTYPE, ZORIGINATINGUITYPE
                    ) VALUES (
                        ?, ?, 1,
                        ?, 1, ?,
                        0, ?, 0,
                        0, ?, 1, 0,
                        ?, ?,
                        ?, NULL, NULL, ?,
                        ?, ?,
                        0, 0, 0, 0, 0, 0
                    )
                    """,
                    (
                        z_pk, CALLRECORD_ENT,
                        answered, call_type,
                        HANDLE_TYPE_PHONE,
                        originated,
                        z_date, float(rec.duration_seconds or 0),
                        rec.number, rec.name,
                        service, str(uuid.uuid4()).upper(),
                    ),
                )
                inserted += 1

            new_max = cur_max + inserted
            con.execute(
                "UPDATE Z_PRIMARYKEY SET Z_MAX=? WHERE Z_NAME=?",
                (new_max, "CallRecord"),
            )
    finally:
        con.close()

    logger.info(
        "inject_calls_ios: staged %d call record(s) into %s (Z_MAX -> %d)",
        inserted, db_path, cur_max + inserted,
    )
    return inserted


def _apple_seconds(ts: datetime | None) -> float:
    if ts is None:
        ts = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.timestamp() - APPLE_EPOCH_OFFSET


def _export_fallback(items: list[CallRecord], staging_dir: Path) -> None:
    staging_dir.mkdir(parents=True, exist_ok=True)

    txt_path = staging_dir / "calls_ios_export.txt"
    try:
        lines = ["iOS Call Log Export (PhoneTransfer)\n", "=" * 60 + "\n"]
        for c in items:
            ts = c.timestamp.isoformat() if c.timestamp else "unknown time"
            lines.append(
                f"[{ts}] {c.call_type:<8} {c.number} ({c.name or '-'}) "
                f"— {c.duration_seconds}s\n"
            )
        txt_path.write_text("".join(lines), encoding="utf-8")
    except Exception as exc:
        logger.error("inject_calls_ios: TXT export failed: %s", exc)

    json_path = staging_dir / "calls_ios_export.json"
    try:
        records = [
            {
                "number": c.number,
                "name": c.name,
                "timestamp": c.timestamp.isoformat() if c.timestamp else None,
                "duration_seconds": c.duration_seconds,
                "call_type": c.call_type,
            }
            for c in items
        ]
        json_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.error("inject_calls_ios: JSON export failed: %s", exc)
