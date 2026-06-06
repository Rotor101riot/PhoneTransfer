"""
inject_reminders_ios.py

Inject reminders into an iOS device.

Two paths:
  * **Backup-mod (default)**: when an :class:`IOSBackupInjector` session is
    active, INSERT rows into the active Reminders Core Data store
    (``AppDomainGroup-group.com.apple.reminders/Container_v1/Stores/
    Data-<UUID>.sqlite``).  iOS 17 "Reminders 2" splits storage into one
    `.sqlite` per account; we discover the active store at runtime by
    enumerating the per-store ``ZREMCDBASELIST`` and picking the one that
    has at least one real user-visible list (``ZSMARTLISTTYPE IS NULL``,
    ``ZDAISIMMUTABLE = 0``).
  * **AFC ICS fallback**: pre-existing path that drops a VTODO ``.ics``
    onto the device for the user to import manually via the Reminders
    app.  Kept for the no-injector case (e.g. user explicitly bypassed
    the orchestrator, or destination is a deferred restore where AFC
    is the only available channel).

Schema notes (iOS 17, ``Z_ENT`` values device-stable across this model):
  - REMCDReminder: Z_ENT = 39, Z_SUPER = 0 (its own root in this schema)
  - REMCDList:     Z_ENT = 3,  Z_SUPER = 2 (REMCDBaseList)
  - REMCDAccount:  Z_ENT = 14, Z_SUPER = 13 (REMCDObject)
  - All timestamps are Apple-epoch seconds (978307200 = 2001-01-01 UTC).
  - ``ZIDENTIFIER`` is 16 bytes of UUID, ``ZCKIDENTIFIER`` is its string
    form — Reminders.app reads both.
  - ``ZACCOUNT`` and ``ZLIST`` are FKs into ZREMCDOBJECT.Z_PK and
    ZREMCDBASELIST.Z_PK respectively.  We reuse the lists already in the
    store (we do not synthesise an account or list).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.ios_backup_injector import IOSBackupInjector, get_current_injector
from core.normalization_schema import Reminder

logger = logging.getLogger(__name__)


_REM_DOMAIN = "AppDomainGroup-group.com.apple.reminders"
_REM_STORES_DIR = "Container_v1/Stores"
_APPLE_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01

_REM_ENT_REMINDER = 39  # ZREMCDREMINDER

# Legacy AFC fallback target
_AFC_DEST_DIR = "/var/mobile/Media/PhoneTransfer"
_AFC_DEST_FILE = f"{_AFC_DEST_DIR}/reminders_import.ics"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def inject(
    device_id: str, items: list[Reminder], staging_dir: Path, is_privileged: bool
) -> int:
    if not items:
        logger.info("No reminders to inject for device %s.", device_id)
        return 0

    injector = get_current_injector()
    if injector is not None:
        return _inject_via_backup(injector, items)

    return _inject_via_afc_ics(device_id, items, staging_dir)


# ---------------------------------------------------------------------------
# Backup-mod path
# ---------------------------------------------------------------------------

def _inject_via_backup(
    injector: IOSBackupInjector, items: list[Reminder]
) -> int:
    store_rel = _pick_active_store(injector)
    if store_rel is None:
        logger.warning(
            "inject_reminders_ios: no Reminders Core Data store with a "
            "real list found in the source backup; nothing to inject into."
        )
        return 0

    db_path = injector.stage_db(_REM_DOMAIN, store_rel)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=FULL")

        list_pk, account_pk = _pick_target_list(con)
        if list_pk is None:
            logger.warning(
                "inject_reminders_ios: store %s has no usable list; skip", store_rel
            )
            return 0

        max_pk_row = con.execute(
            "SELECT COALESCE(MAX(Z_PK), 0) FROM ZREMCDREMINDER"
        ).fetchone()
        max_pk = int(max_pk_row[0])

        z_max_row = con.execute(
            "SELECT Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME = 'REMCDReminder'"
        ).fetchone()
        z_max = int(z_max_row[0]) if z_max_row else max_pk

        inserted = 0
        with con:
            for i, rem in enumerate(items, start=1):
                new_pk = max(max_pk, z_max) + i

                created = _to_apple_epoch(getattr(rem, "created", None))
                modified = _to_apple_epoch(getattr(rem, "modified", None) or datetime.now(timezone.utc))
                due = _to_apple_epoch(rem.due) if rem.due else None
                completed_at = (
                    _to_apple_epoch(datetime.now(timezone.utc))
                    if rem.completed else None
                )

                ck_uuid = (rem.uid or str(uuid.uuid4())).upper()
                # Keep the UUID bytes consistent with the string form when
                # the caller supplied one; otherwise generate fresh.
                try:
                    id_bytes = uuid.UUID(ck_uuid).bytes
                except ValueError:
                    new_uuid = uuid.uuid4()
                    ck_uuid = str(new_uuid).upper()
                    id_bytes = new_uuid.bytes

                con.execute(
                    """
                    INSERT INTO ZREMCDREMINDER (
                        Z_PK, Z_ENT, Z_OPT,
                        ZALLDAY, ZCOMPLETED, ZFLAGGED, ZPRIORITY,
                        ZMARKEDFORDELETION,
                        ZACCOUNT, ZLIST,
                        ZCREATIONDATE, ZLASTMODIFIEDDATE,
                        ZDUEDATE, ZDISPLAYDATEDATE, ZCOMPLETIONDATE,
                        ZTITLE, ZNOTES,
                        ZCKIDENTIFIER, ZIDENTIFIER
                    ) VALUES (
                        ?, ?, 1,
                        0, ?, 0, ?,
                        0,
                        ?, ?,
                        ?, ?,
                        ?, ?, ?,
                        ?, ?,
                        ?, ?
                    )
                    """,
                    (
                        new_pk, _REM_ENT_REMINDER,
                        1 if rem.completed else 0,
                        max(0, min(9, int(rem.priority or 0))),
                        account_pk, list_pk,
                        created, modified,
                        due, due, completed_at,
                        rem.title or "Reminder",
                        rem.notes,
                        ck_uuid, id_bytes,
                    ),
                )
                inserted += 1

            new_max = max(z_max, max_pk + inserted)
            con.execute(
                "UPDATE Z_PRIMARYKEY SET Z_MAX = ? WHERE Z_NAME = 'REMCDReminder'",
                (new_max,),
            )

    finally:
        con.close()

    logger.info(
        "inject_reminders_ios: staged %d reminder(s) into %s (list_pk=%d, account_pk=%d)",
        inserted, store_rel, list_pk, account_pk,
    )
    return inserted


# ---------------------------------------------------------------------------
# Active-store discovery
# ---------------------------------------------------------------------------

_STORE_RE = re.compile(r"Data-[0-9A-F-]+\.sqlite$", re.IGNORECASE)


def _pick_active_store(injector: IOSBackupInjector) -> str | None:
    """Return the relativePath of the Reminders store with a real user list.

    Modern iOS keeps one ``Data-<UUID>.sqlite`` per account; only one of
    them holds the user's actual lists & reminders.  We open each candidate
    in turn and keep the first that has a usable list.  Side effect: each
    candidate gets staged via :meth:`stage_db`, which also registers it as
    an override; that's fine for the chosen one and harmless for the
    others (we'll just write back identical bytes).
    """
    candidates = injector.list_relative_paths(
        _REM_DOMAIN, f"{_REM_STORES_DIR}/Data-%.sqlite"
    )
    candidates = [c for c in candidates if _STORE_RE.search(c)]

    for rel in candidates:
        try:
            local = injector.stage_db(_REM_DOMAIN, rel)
        except Exception:
            logger.debug("Could not stage %s; skipping", rel, exc_info=True)
            continue
        try:
            con = sqlite3.connect(str(local))
            try:
                tabs = {
                    r[0] for r in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if "ZREMCDREMINDER" not in tabs or "ZREMCDBASELIST" not in tabs:
                    continue
                cols = {r[1] for r in con.execute(
                    "PRAGMA table_info(ZREMCDBASELIST)"
                )}
                # iOS schema variations: probe defensively.
                where_bits = []
                if "ZSMARTLISTTYPE" in cols:
                    where_bits.append("ZSMARTLISTTYPE IS NULL")
                if "ZDAISIMMUTABLE" in cols:
                    where_bits.append("COALESCE(ZDAISIMMUTABLE, 0) = 0")
                if "ZDAISNOTIFICATIONSCOLLECTION" in cols:
                    where_bits.append(
                        "COALESCE(ZDAISNOTIFICATIONSCOLLECTION, 0) = 0"
                    )
                where = " AND ".join(where_bits) or "1=1"
                cnt = con.execute(
                    f"SELECT COUNT(*) FROM ZREMCDBASELIST WHERE {where}"
                ).fetchone()[0]
                if cnt > 0:
                    logger.info(
                        "inject_reminders_ios: active store = %s (%d real list(s))",
                        rel, cnt,
                    )
                    return rel
            finally:
                con.close()
        except sqlite3.Error:
            logger.debug("sqlite open failed for %s", rel, exc_info=True)
            continue

    return None


def _pick_target_list(con: sqlite3.Connection) -> tuple[int | None, int | None]:
    """Pick the user-visible list to attach reminders to (and its account)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(ZREMCDBASELIST)")}
    where_bits = []
    if "ZSMARTLISTTYPE" in cols:
        where_bits.append("ZSMARTLISTTYPE IS NULL")
    if "ZDAISIMMUTABLE" in cols:
        where_bits.append("COALESCE(ZDAISIMMUTABLE, 0) = 0")
    if "ZDAISNOTIFICATIONSCOLLECTION" in cols:
        where_bits.append("COALESCE(ZDAISNOTIFICATIONSCOLLECTION, 0) = 0")
    where = " AND ".join(where_bits) or "1=1"

    row = con.execute(
        f"SELECT Z_PK, ZACCOUNT FROM ZREMCDBASELIST WHERE {where} "
        "ORDER BY Z_PK LIMIT 1"
    ).fetchone()
    if not row:
        return None, None
    return int(row[0]), int(row[1] or 0)


# ---------------------------------------------------------------------------
# Apple-epoch helpers
# ---------------------------------------------------------------------------

def _to_apple_epoch(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() - _APPLE_EPOCH_OFFSET


# ---------------------------------------------------------------------------
# Legacy ICS fallback (pre-existing path)
# ---------------------------------------------------------------------------

def _format_dt(dt) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _escape_ics(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
        .replace("\r", "")
    )


def _build_ics(items: list[Reminder]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//PhoneTransfer//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for reminder in items:
        uid = reminder.uid or str(uuid.uuid4())
        title = _escape_ics(reminder.title or "Reminder")
        notes = _escape_ics(reminder.notes or "") if reminder.notes else ""
        status = "COMPLETED" if reminder.completed else "NEEDS-ACTION"
        priority = max(0, min(9, int(reminder.priority or 0)))

        lines += [
            "BEGIN:VTODO",
            f"UID:{uid}",
            f"SUMMARY:{title}",
        ]
        if reminder.due:
            lines.append(f"DUE:{_format_dt(reminder.due)}")
        if notes:
            lines.append(f"DESCRIPTION:{notes}")
        lines += [
            f"STATUS:{status}",
            f"PRIORITY:{priority}",
            "END:VTODO",
        ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def _inject_via_afc_ics(
    device_id: str, items: list[Reminder], staging_dir: Path
) -> int:
    staging_dir.mkdir(parents=True, exist_ok=True)
    local_ics = staging_dir / "reminders_import.ics"

    try:
        ics_content = _build_ics(items)
        local_ics.write_text(ics_content, encoding="utf-8")
        logger.info("Generated ICS with %d VTODO(s) at %s", len(items), local_ics)
    except Exception as exc:
        logger.error("Failed to generate ICS file: %s", exc)
        return 0

    try:
        from core.device_connection_cache import get_broker
        from core.afc_connector import AFCConnector

        broker = get_broker(device_id)
        afc = AFCConnector(broker)

        afc.makedirs(_AFC_DEST_DIR)
        afc.write_file(_AFC_DEST_FILE, local_ics.read_bytes())
        logger.info(
            "Pushed reminders ICS to %s on device %s. "
            "Open this file on the iOS device to import reminders into the Reminders app.",
            _AFC_DEST_FILE,
            device_id,
        )
        return len(items)
    except Exception as exc:
        logger.error(
            "Failed to push reminders ICS to device %s: %s", device_id, exc
        )
        return 0
