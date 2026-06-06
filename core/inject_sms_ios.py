"""
inject_sms_ios.py

Inject SMS / iMessage conversations into an iOS device.

Strategy (preferred): if an :class:`IOSBackupInjector` is active on the
current pipeline, extract ``HomeDomain:Library/SMS/sms.db`` from the
encrypted backup, INSERT new rows (handle + chat + message +
chat_handle_join + chat_message_join), and register the modified DB as
an override.  The pipeline's commit pass re-encrypts it and a subsequent
``pymobiledevice3 backup2 restore`` writes it back to the device.

Fallback: if no injector is active (e.g. destination is iOS but the
pipeline could not prepare an encrypted backup), the messages are
exported to JSON and TXT under the staging dir as a last-resort record.

Schema reference (ported from G:/test/modify_sms.py):
  - ``message.date`` is Apple-epoch NANOSECONDS, not seconds.
  - ``chat.style = 45`` for 1:1 chats.
  - A ``verify_chat`` SQLite function must be shimmed so the
    BEFORE-INSERT trigger doesn't explode.
  - Bulk ``INSERT`` into ``handle``/``chat``/``message`` is safe; the
    UNIQUE constraints we might hit (``handle(id, service)``,
    ``chat(guid)``) are handled by grouping by address + skipping
    duplicates.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.ios_backup_injector import IOSBackupInjector, get_current_injector
from core.normalization_schema import Message

logger = logging.getLogger(__name__)


SMS_DB_DOMAIN = "HomeDomain"
SMS_DB_RELPATH = "Library/SMS/sms.db"

APPLE_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01 UTC
CHAT_STYLE_1TO1 = 45
CHAT_STATE_NORMAL = 3

# Fallback device-owner number/account used when the source messages don't
# include a ``destination_caller_id`` of their own.  The real values can be
# supplied via ``staging_dir/sms_ios_account.json`` (see _load_account_info).
DEFAULT_ACCOUNT_PHONE = ""


def inject(
    device_id: str,
    items: list[Message],
    staging_dir: Path,
    is_privileged: bool,
) -> int:
    """
    Inject SMS/iMessage rows into ``sms.db`` via the active backup session.

    Returns the number of messages successfully staged.  When no backup
    injector is active, writes a JSON + TXT export and returns 0.
    """
    if not items:
        logger.info("inject_sms_ios: no messages to inject for %s", device_id)
        return 0

    injector = get_current_injector()
    if injector is None:
        logger.warning(
            "inject_sms_ios: no backup injector active — falling back to "
            "JSON/TXT export under %s", staging_dir,
        )
        _export_fallback(items, staging_dir)
        return 0

    account = _load_account_info(staging_dir)
    return _inject_via_backup(injector, items, account)


# ---------------------------------------------------------------------------
# backup-mod injection
# ---------------------------------------------------------------------------

def _inject_via_backup(
    injector: IOSBackupInjector,
    messages: list[Message],
    account: dict,
) -> int:
    db_path = injector.stage_db(SMS_DB_DOMAIN, SMS_DB_RELPATH)
    device_phone = account.get("device_phone") or DEFAULT_ACCOUNT_PHONE
    account_guid = account.get("account_guid") or str(uuid.uuid4()).upper()
    sms_account = f"P:{device_phone}" if device_phone else "P:"

    # Group messages by the other party's number so each conversation becomes
    # one handle + one chat + N message rows.
    by_other: dict[str, list[Message]] = {}
    for m in messages:
        other = m.recipient if m.is_sent else m.sender
        if not other:
            continue
        by_other.setdefault(other, []).append(m)

    if not by_other:
        logger.info("inject_sms_ios: no messages had a resolvable counterpart")
        return 0

    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=FULL")
        # Shim the trigger function iOS registers natively but desktop SQLite
        # doesn't know about.  See modify_sms.py for context.
        con.create_function(
            "verify_chat", 1, lambda _guid: None, deterministic=True
        )

        inserted = 0
        with con:
            for address, msgs in by_other.items():
                handle_id = _get_or_create_handle(con, address)
                chat_id = _get_or_create_chat(
                    con, address, account_guid, sms_account
                )
                _ensure_chat_handle_join(con, chat_id, handle_id)

                for msg in msgs:
                    date_ns = _ns_from_datetime(msg.timestamp)
                    msg_id = _insert_message(
                        con,
                        text=msg.body or "",
                        handle_id=handle_id,
                        is_from_me=1 if msg.is_sent else 0,
                        date_ns=date_ns,
                        service=(msg.service or "sms").upper(),
                        sms_account=sms_account,
                        account_guid=account_guid,
                        device_phone=device_phone,
                    )
                    _insert_chat_message_join(con, chat_id, msg_id, date_ns)
                    inserted += 1
    finally:
        con.close()

    logger.info(
        "inject_sms_ios: staged %d message(s) across %d chat(s) into %s",
        inserted, len(by_other), db_path,
    )
    return inserted


def _get_or_create_handle(con: sqlite3.Connection, address: str) -> int:
    row = con.execute(
        "SELECT ROWID FROM handle WHERE id=? AND service='SMS' LIMIT 1",
        (address,),
    ).fetchone()
    if row:
        return row[0]
    cur = con.execute(
        "INSERT INTO handle (id, country, service, uncanonicalized_id, "
        "person_centric_id) VALUES (?, NULL, 'SMS', NULL, NULL)",
        (address,),
    )
    return cur.lastrowid


def _get_or_create_chat(
    con: sqlite3.Connection,
    address: str,
    account_guid: str,
    sms_account: str,
) -> int:
    guid = f"SMS;-;{address}"
    row = con.execute(
        "SELECT ROWID FROM chat WHERE guid=? LIMIT 1", (guid,)
    ).fetchone()
    if row:
        return row[0]
    cur = con.execute(
        """
        INSERT INTO chat (
            guid, style, state, account_id, chat_identifier, service_name,
            account_login, is_archived, is_filtered, successful_query,
            ck_sync_state, last_read_message_timestamp, is_blackholed,
            syndication_date, syndication_type, is_recovered,
            is_deleting_incoming_messages, is_pending_review
        ) VALUES (?, ?, ?, ?, ?, 'SMS', ?, 0, 0, 1,
                  0, 0, 0, 0, 0, 0, 0, 0)
        """,
        (guid, CHAT_STYLE_1TO1, CHAT_STATE_NORMAL,
         account_guid, address, sms_account),
    )
    return cur.lastrowid


def _ensure_chat_handle_join(
    con: sqlite3.Connection, chat_id: int, handle_id: int
) -> None:
    exists = con.execute(
        "SELECT 1 FROM chat_handle_join WHERE chat_id=? AND handle_id=? LIMIT 1",
        (chat_id, handle_id),
    ).fetchone()
    if exists:
        return
    con.execute(
        "INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (?, ?)",
        (chat_id, handle_id),
    )


def _insert_message(
    con: sqlite3.Connection,
    *,
    text: str,
    handle_id: int,
    is_from_me: int,
    date_ns: int,
    service: str,
    sms_account: str,
    account_guid: str,
    device_phone: str,
) -> int:
    guid = str(uuid.uuid4()).upper()
    cur = con.execute(
        """
        INSERT INTO message (
            guid, text, replace, service_center, handle_id, subject, country,
            attributedBody, version, type, service, account, account_guid,
            error, date, date_read, date_delivered,
            is_delivered, is_finished, is_emote, is_from_me, is_empty,
            is_delayed, is_auto_reply, is_prepared, is_read, is_system_message,
            is_sent, has_dd_results, is_service_message, is_forward,
            was_downgraded, is_archive, cache_has_attachments, cache_roomnames,
            was_data_detected, was_deduplicated, is_audio_message, is_played,
            date_played, item_type, other_handle, group_title,
            group_action_type, share_status, share_direction,
            is_expirable, expire_state, message_action_type, message_source,
            associated_message_guid, associated_message_type, balloon_bundle_id,
            payload_data, expressive_send_style_id,
            associated_message_range_location, associated_message_range_length,
            time_expressive_send_played, message_summary_info, ck_sync_state,
            destination_caller_id, is_corrupt, reply_to_guid, sort_id,
            is_spam, has_unseen_mention, thread_originator_guid,
            thread_originator_part, was_delivered_quietly, did_notify_recipient,
            was_detonated, part_count, is_stewie, is_kt_verified, is_sos,
            is_critical, is_pending_satellite_send, needs_relay,
            schedule_type, schedule_state, sent_or_received_off_grid,
            date_recovered, is_time_sensitive, index_state
        ) VALUES (
            ?, ?, 0, NULL, ?, NULL, NULL,
            NULL, 10, 0, ?, ?, ?,
            0, ?, ?, ?,
            1, 1, 0, ?, 0,
            0, 0, 0, 1, 0,
            ?, 0, 0, 0,
            0, 0, 0, NULL,
            0, 0, 0, 0,
            NULL, 0, 0, NULL,
            0, 0, 0,
            0, 0, 0, 0,
            NULL, 0, NULL,
            NULL, NULL,
            0, 0,
            NULL, NULL, 0,
            ?, 0, NULL, ?,
            0, 0, NULL,
            NULL, 0, 0,
            0, 1, 0, 0, 0,
            0, 0, 0,
            0, 0, 0,
            0, 0, 0
        )
        """,
        (
            guid, text, handle_id,
            service, sms_account, account_guid,
            date_ns, date_ns, date_ns,
            is_from_me,
            is_from_me,                 # is_sent
            device_phone,               # destination_caller_id
            date_ns,                    # sort_id
        ),
    )
    return cur.lastrowid


def _insert_chat_message_join(
    con: sqlite3.Connection, chat_id: int, message_id: int, date_ns: int
) -> None:
    con.execute(
        "INSERT INTO chat_message_join "
        "(chat_id, message_id, message_date, index_state) VALUES (?, ?, ?, 0)",
        (chat_id, message_id, date_ns),
    )


def _ns_from_datetime(ts: datetime | None) -> int:
    if ts is None:
        ts = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    epoch_seconds = ts.timestamp() - APPLE_EPOCH_OFFSET
    return int(epoch_seconds * 1_000_000_000)


# ---------------------------------------------------------------------------
# Account info & fallback
# ---------------------------------------------------------------------------

def _load_account_info(staging_dir: Path) -> dict:
    """
    Look for ``<staging>/sms_ios_account.json`` with the destination device's
    SMS account settings.  The extractor side is expected to drop this file
    so we don't have to re-derive the owner's phone number / account GUID
    from the backup itself.  Returns an empty dict if absent.
    """
    path = staging_dir / "sms_ios_account.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("inject_sms_ios: couldn't parse %s: %s", path, exc)
        return {}


def _export_fallback(items: list[Message], staging_dir: Path) -> None:
    staging_dir.mkdir(parents=True, exist_ok=True)

    txt_path = staging_dir / "sms_ios_export.txt"
    try:
        lines = ["iOS SMS Export (PhoneTransfer)\n", "=" * 60 + "\n"]
        for msg in items:
            ts = msg.timestamp.isoformat() if msg.timestamp else "unknown time"
            direction = "to" if msg.is_sent else "from"
            other = msg.recipient if msg.is_sent else msg.sender
            lines.append(f"[{ts}] {direction} <{other}>: {msg.body}\n")
        txt_path.write_text("".join(lines), encoding="utf-8")
    except Exception as exc:
        logger.error("inject_sms_ios: failed writing TXT export: %s", exc)

    json_path = staging_dir / "sms_ios_export.json"
    try:
        records = [
            {
                "platform_id": m.platform_id,
                "sender": m.sender,
                "recipient": m.recipient,
                "body": m.body,
                "timestamp": m.timestamp.isoformat() if m.timestamp else None,
                "is_sent": m.is_sent,
                "service": m.service,
                "read": m.read,
            }
            for m in items
        ]
        json_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.error("inject_sms_ios: failed writing JSON export: %s", exc)
