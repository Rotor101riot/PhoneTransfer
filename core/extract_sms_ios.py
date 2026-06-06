"""
extract_sms_ios.py

Extracts SMS, MMS, and iMessage records from an iOS device and returns a
list of Message objects defined in normalization_schema.py.

Strategy
--------
1. Pull sms.db via AFC2 (jailbroken) or iOSbackup (non-jailbroken).
2. Query the `message`, `handle`, and `attachment` tables.
3. For each message, determine sender/recipient, service, and timestamp.
4. Optionally pull attachment files from the MediaDomain via AFC.

Timestamp note
--------------
iOS sms.db uses Apple epoch (seconds since 2001-01-01) for older iOS
versions, but iOS 11+ stores nanoseconds since 2001-01-01.  We detect
which scale is in use by magnitude: values > 1e10 are nanoseconds.

Never raises — all exceptions are caught, logged, and return partial/empty
results.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.normalization_schema import Message, MessageAttachment

logger = logging.getLogger(__name__)

# Apple epoch offset
_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# Device / backup paths
_DB_DEVICE_PATH = "/var/mobile/Library/SMS/sms.db"
_DB_RELATIVE_PATH = "Library/SMS/sms.db"
_IOS_BACKUP_DOMAIN = "AppDomain-com.apple.MobileSMS"

# Attachment base path in MediaDomain backup
_ATTACH_DOMAIN = "MediaDomain"
_ATTACH_DIR_PREFIX = "Library/SMS/Attachments"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(udid: str, staging_dir: Path, is_jailbroken: bool = False) -> list[Message]:
    """
    Extract all SMS/MMS/iMessage records from the iOS device identified by
    *udid*.

    Parameters
    ----------
    udid:           iOS device UDID.
    staging_dir:    Local directory used for temporary file copies.
    is_jailbroken:  Whether the device has AFC2 available.

    Returns
    -------
    list[Message]   Possibly empty on total failure.
    """
    try:
        return _extract_impl(udid, staging_dir, is_jailbroken)
    except Exception as exc:
        logger.exception("extract_sms_ios: top-level failure for %s: %s", udid, exc)
        return []


def _extract_impl(udid: str, staging_dir: Path, is_jailbroken: bool) -> list[Message]:
    work_dir = staging_dir / "sms_ios"
    work_dir.mkdir(parents=True, exist_ok=True)

    db_path = _pull_sms_db(udid, work_dir, is_jailbroken)
    if db_path is None:
        logger.warning("sms_ios: could not obtain sms.db for %s", udid)
        return []

    messages = _parse_sms_db(udid, db_path, work_dir, is_jailbroken)
    logger.info("sms_ios: extracted %d messages for %s", len(messages), udid)
    return messages


# ---------------------------------------------------------------------------
# Pull sms.db
# ---------------------------------------------------------------------------

def _pull_sms_db(udid: str, work_dir: Path, is_jailbroken: bool) -> Path | None:
    local_db = work_dir / "sms.db"

    if is_jailbroken:
        try:
            from core.device_connection_cache import get_broker
            from core.afc2_connector import AFC2Connector

            broker = get_broker(udid)
            afc2 = AFC2Connector(broker)
            ok = afc2.pull_file(_DB_DEVICE_PATH, local_db)
            if ok and local_db.exists():
                logger.debug("sms_ios: pulled sms.db via AFC2")
                return local_db
        except PermissionError:
            logger.warning("sms_ios: AFC2 not available despite is_jailbroken=True")
        except Exception as exc:
            logger.warning("sms_ios: AFC2 pull failed: %s", exc)

    return _pull_via_iosbackup(udid, _DB_RELATIVE_PATH, _IOS_BACKUP_DOMAIN, local_db)


def _pull_via_iosbackup(udid: str, relative_path: str, domain: str, dest: Path) -> Path | None:
    try:
        from core.device_connection_cache import get_iosbackup
        from core.iosbackup_helpers import fix_truncated_sqlite
        dest.parent.mkdir(parents=True, exist_ok=True)
        backup = get_iosbackup(udid)
        info = backup.getFileDecryptedCopy(
            relativePath=relative_path,
            targetName=dest.name,
            targetFolder=str(dest.parent),
        )
        if info and dest.exists():
            fix_truncated_sqlite(dest, backup, relative_path)
            logger.debug("sms_ios: pulled %s via iOSbackup", relative_path)
            return dest
    except Exception as exc:
        logger.warning("sms_ios: iOSbackup pull failed for %s: %s", relative_path, exc)

    return None


# ---------------------------------------------------------------------------
# Timestamp conversion
# ---------------------------------------------------------------------------

def _apple_ts_to_datetime(ts: float | int | None) -> datetime:
    """
    Convert an iOS timestamp to a UTC datetime.

    sms.db timestamps are Apple epoch (since 2001-01-01).
    iOS 11+ uses nanoseconds; older versions use seconds.
    Values > 1e10 are treated as nanoseconds.
    """
    if ts is None or ts == 0:
        return _APPLE_EPOCH

    ts = float(ts)
    if ts > 1e10:
        ts = ts / 1_000_000_000.0  # nanoseconds → seconds

    try:
        return _APPLE_EPOCH + timedelta(seconds=ts)
    except (OverflowError, OSError, ValueError):
        return _APPLE_EPOCH


# ---------------------------------------------------------------------------
# Parse sms.db
# ---------------------------------------------------------------------------

def _parse_sms_db(
    udid: str, db_path: Path, work_dir: Path, is_jailbroken: bool
) -> list[Message]:
    messages: list[Message] = []

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row

            # Build handle_id → phone/email map
            handle_map: dict[int, str] = {}
            try:
                cur = conn.execute("SELECT ROWID, id FROM handle")
                for row in cur.fetchall():
                    handle_map[row["ROWID"]] = row["id"] or ""
            except sqlite3.OperationalError as exc:
                logger.warning("sms_ios: handle table query failed: %s", exc)

            # Build message_id → chat_identifier map (group chat support)
            # chat.chat_identifier is the group name or individual JID
            chat_map: dict[int, str] = {}
            try:
                cur = conn.execute(
                    """
                    SELECT cmj.message_id, c.chat_identifier
                    FROM chat_message_join cmj
                    JOIN chat c ON c.ROWID = cmj.chat_id
                    """
                )
                for row in cur.fetchall():
                    chat_map[row["message_id"]] = row["chat_identifier"] or ""
            except sqlite3.OperationalError as exc:
                logger.debug("sms_ios: chat_message_join query failed (older schema?): %s", exc)

            # Discover optional message columns
            try:
                col_info = conn.execute("PRAGMA table_info(message)").fetchall()
                msg_cols = {r[1].upper() for r in col_info}
            except Exception:
                msg_cols = set()

            has_deleted   = "IS_DELETED"   in msg_cols
            has_subject   = "SUBJECT"      in msg_cols
            has_read      = "READ"         in msg_cols
            # iOS 16+ columns: is_retracted (sender deleted for everyone),
            # date_edited (message was edited after send)
            has_retracted = "IS_RETRACTED" in msg_cols
            has_edited    = "DATE_EDITED"  in msg_cols
            # iOS 18+ RCS column
            has_rcs       = "IS_RCS"       in msg_cols

            # Build message_id → list[attachment] map
            attachment_map = _build_attachment_map(
                udid, conn, work_dir, is_jailbroken
            )

            # Build main SELECT dynamically
            select_cols = [
                "m.ROWID",
                "m.text",
                "m.date",
                "m.is_from_me",
                "m.service",
                "m.handle_id",
                "m.cache_has_attachments",
            ]
            if has_read:
                select_cols.append("m.read")
            if has_rcs:
                select_cols.append("m.is_rcs")
            # Build WHERE clause: skip soft-deleted and retracted messages
            where_parts = []
            if has_deleted:
                select_cols.append("m.is_deleted")
                where_parts.append("(m.is_deleted IS NULL OR m.is_deleted = 0)")
            if has_retracted:
                select_cols.append("m.is_retracted")
                where_parts.append("(m.is_retracted IS NULL OR m.is_retracted = 0)")
            where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
            if has_subject:
                select_cols.append("m.subject")
            if has_edited:
                select_cols.append("m.date_edited")

            query = (
                f"SELECT {', '.join(select_cols)} FROM message m "
                f"{where_clause} ORDER BY m.date ASC"
            )

            try:
                cur = conn.execute(query)
            except sqlite3.OperationalError as exc:
                logger.error("sms_ios: message table query failed: %s", exc)
                return []

            for row in cur.fetchall():
                try:
                    msg = _row_to_message(
                        row, handle_map, attachment_map, chat_map,
                        has_subject=has_subject,
                        has_edited=has_edited,
                        has_read=has_read,
                        has_rcs=has_rcs,
                    )
                    messages.append(msg)
                except Exception as exc:
                    logger.debug("sms_ios: skipping message row %s: %s", row["ROWID"], exc)

    except Exception as exc:
        logger.exception("sms_ios: failed to parse sms.db: %s", exc)

    return messages


def _row_to_message(
    row: sqlite3.Row,
    handle_map: dict[int, str],
    attachment_map: dict[int, list[MessageAttachment]],
    chat_map: dict[int, str],
    *,
    has_subject: bool,
    has_edited: bool = False,
    has_read: bool = True,
    has_rcs: bool = False,
) -> Message:
    rowid = row["ROWID"]
    is_sent = bool(row["is_from_me"])
    handle_id = row["handle_id"] or 0
    peer = handle_map.get(handle_id, "unknown")
    ts = _apple_ts_to_datetime(row["date"])
    read = bool(row["read"]) if has_read and row["read"] is not None else True
    is_rcs = has_rcs and bool(row["is_rcs"])

    # Prefer chat_identifier for group chats (provides the group JID/name)
    chat_id = chat_map.get(rowid, "")
    recipient_id = chat_id if chat_id else peer

    # Combine subject + body (MMS messages often have a subject line)
    body = row["text"] or ""
    if has_subject and row["subject"]:
        subject = row["subject"].strip()
        if subject:
            body = f"{subject}\n{body}" if body else subject

    # iOS 16+: note when a message was edited after sending
    if has_edited and row["date_edited"]:
        edited_ts = _apple_ts_to_datetime(row["date_edited"])
        edited_str = edited_ts.strftime("%Y-%m-%d %H:%M:%S UTC") if edited_ts != _APPLE_EPOCH else "?"
        body = f"{body} [edited {edited_str}]" if body else f"[edited {edited_str}]"

    service_raw = (row["service"] or "").lower()
    if "imessage" in service_raw:
        service = "imessage"
    elif is_rcs:
        service = "rcs"
    elif "mms" in service_raw:
        service = "mms"
    else:
        service = "sms"

    attachments = attachment_map.get(rowid, [])
    # If there are attachments and no text body, label as MMS
    if attachments and service == "sms":
        service = "mms"  # type: ignore[assignment]

    sender = "self" if is_sent else peer
    recipient = recipient_id if is_sent else "self"

    return Message(
        platform_id=str(rowid),
        sender=sender,
        recipient=recipient,
        body=body,
        timestamp=ts,
        is_sent=is_sent,
        attachments=attachments,
        service=service,  # type: ignore[arg-type]
        read=read,
    )


# ---------------------------------------------------------------------------
# Attachment extraction
# ---------------------------------------------------------------------------

def _build_attachment_map(
    udid: str,
    conn: sqlite3.Connection,
    work_dir: Path,
    is_jailbroken: bool,
) -> dict[int, list[MessageAttachment]]:
    """
    Return a dict mapping message ROWID to a list of MessageAttachment objects.
    Attempts to pull actual attachment files to work_dir/attachments/.
    """
    result: dict[int, list[MessageAttachment]] = {}
    attach_dir = work_dir / "attachments"

    # query the message_attachment join table and attachment table
    try:
        # iOS 7+: join via message_attachment table
        cur = conn.execute(
            """
            SELECT
                ma.message_id,
                a.ROWID as attach_id,
                a.filename,
                a.mime_type,
                a.transfer_name
            FROM attachment a
            JOIN message_attachment_join ma ON a.ROWID = ma.attachment_id
            """
        )
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        # Older schema: direct handle_id / attachment link
        try:
            cur = conn.execute(
                "SELECT ROWID, message_id, filename, mime_type, transfer_name FROM attachment"
            )
            rows = cur.fetchall()
        except sqlite3.OperationalError as exc:
            logger.debug("sms_ios: attachment table not found: %s", exc)
            return result

    afc_connector = _get_afc_connector_for_attachments(udid, is_jailbroken)

    try:
        for row in rows:
            msg_id = row["message_id"]
            filename = row["transfer_name"] or row["filename"] or f"attach_{row['attach_id']}"
            mime = row["mime_type"] or "application/octet-stream"
            device_path = row["filename"] or ""

            local_path: Path | None = None
            if device_path and afc_connector is not None:
                local_path = _pull_attachment(afc_connector, device_path, attach_dir, filename)
            elif device_path:
                local_path = _pull_attachment_iosbackup(udid, device_path, attach_dir, filename)

            att = MessageAttachment(
                filename=filename,
                mime_type=mime,
                local_path=local_path,
            )
            result.setdefault(msg_id, []).append(att)
    finally:
        if afc_connector is not None:
            try:
                afc_connector.close()
            except Exception:
                pass

    return result


def _get_afc_connector_for_attachments(udid: str, is_jailbroken: bool):
    """
    Return an AFC2Connector (jailbroken) or None.
    Standard AFC cannot reach /var/mobile/Library/SMS/Attachments directly
    because that path is outside /var/mobile/Media.  On non-jailbroken devices
    we rely on the iOSbackup fallback.
    """
    if not is_jailbroken:
        return None
    try:
        from core.device_connection_cache import get_broker
        from core.afc2_connector import AFC2Connector

        broker = get_broker(udid)
        return AFC2Connector(broker)
    except Exception as exc:
        logger.debug("sms_ios: cannot get AFC2 for attachments: %s", exc)
        return None


def _pull_attachment(connector, device_path: str, attach_dir: Path, filename: str) -> Path | None:
    """Pull one attachment file via AFC2."""
    try:
        safe_name = Path(filename).name
        local_path = attach_dir / safe_name
        local_path.parent.mkdir(parents=True, exist_ok=True)
        ok = connector.pull_file(device_path, local_path)
        return local_path if ok else None
    except Exception as exc:
        logger.debug("sms_ios: attachment pull failed for %s: %s", device_path, exc)
        return None


def _pull_attachment_iosbackup(
    udid: str, device_path: str, attach_dir: Path, filename: str
) -> Path | None:
    """
    Attempt to pull an attachment from an iOS backup.
    device_path in sms.db typically looks like:
        ~/Library/SMS/Attachments/xx/yy/filename.ext
    We convert that to a relative path suitable for iOSbackup.
    """
    try:
        # Strip the leading ~/  or /var/mobile/ prefix
        rel = device_path
        for prefix in ("~/", "/var/mobile/"):
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
                break

        safe_name = Path(filename).name
        local_path = attach_dir / safe_name
        attach_dir.mkdir(parents=True, exist_ok=True)

        from core.device_connection_cache import get_iosbackup
        backup = get_iosbackup(udid)
        info = backup.getFileDecryptedCopy(
            relativePath=rel,
            targetName=safe_name,
            targetFolder=str(attach_dir),
        )
        if info and local_path.exists():
            return local_path
    except Exception as exc:
        logger.debug("sms_ios: iOSbackup attachment pull failed for %s: %s", device_path, exc)

    return None
