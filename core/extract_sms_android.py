"""
extract_sms_android.py

Extracts SMS and MMS messages from an Android device connected via ADB.

Two extraction paths:
- Non-rooted: uses Android content providers via `adb shell content query`
  to read the `sms` and `mms` URIs without requiring root access.
- Rooted: copies mmssms.db directly from the telephony provider data
  directory to /sdcard/, pulls it locally, and parses via sqlite3.
  Falls back to the content provider path on any failure.

Returns a list of Message objects as defined in normalization_schema.py.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.adb_manager import ADBManager
from core.config_loader import get_config
from core.normalization_schema import Message, MessageAttachment

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Content provider URIs
# ---------------------------------------------------------------------------

_URI_SMS = "content://sms"
_URI_MMS = "content://mms"

# ---------------------------------------------------------------------------
# SMS type constants (Android Telephony.Sms.*)
# ---------------------------------------------------------------------------

_TYPE_INBOX  = 1
_TYPE_SENT   = 2
_TYPE_DRAFT  = 3
_TYPE_OUTBOX = 4
_TYPE_FAILED = 5
_TYPE_QUEUED = 6

# Types that count as "sent" for sender/recipient resolution
_SENT_TYPES = {_TYPE_SENT, _TYPE_DRAFT, _TYPE_OUTBOX, _TYPE_FAILED, _TYPE_QUEUED}

# ---------------------------------------------------------------------------
# MMS msg_box constants (Android Telephony.BaseMmsColumns.*)
# ---------------------------------------------------------------------------

_MMS_BOX_INBOX = 1
_MMS_BOX_SENT = 2

# ---------------------------------------------------------------------------
# Remote DB locations
# ---------------------------------------------------------------------------

_REMOTE_DB = (
    "/data/data/com.android.providers.telephony/databases/mmssms.db"
)
_REMOTE_TMP = "/sdcard/mmssms_tmp.db"
_LOCAL_DB_NAME = "mmssms.db"

# Staging sub-directory
_SUBDIR = "sms_android"

# Placeholder used when no phone number is available
_SELF = "self"


# ---------------------------------------------------------------------------
# Content row parser (duplicated per-module to keep modules self-contained)
# ---------------------------------------------------------------------------

def _parse_content_rows(output: str | None) -> list[dict[str, str]]:
    """
    Parse the stdout of `adb shell content query` into a list of dicts.

    Handles values that contain commas by splitting only at ", key=" boundaries.
    Returns an empty list when *output* is None or empty (e.g. after a
    UnicodeDecodeError in the ADB reader thread).
    """
    if not output:
        return []
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


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(
    serial: str,
    staging_dir: Path,
    is_rooted: bool = False,
) -> list[Message]:
    """
    Extract all SMS and MMS messages from the Android device.

    Parameters
    ----------
    serial:      ADB device serial string.
    staging_dir: Root staging directory for this transfer session.
    is_rooted:   If True, attempt direct DB pull first.

    Returns
    -------
    List of Message objects; empty list on any fatal error.
    """
    try:
        sub = staging_dir / _SUBDIR
        sub.mkdir(parents=True, exist_ok=True)

        adb = ADBManager(get_config())

        if is_rooted:
            messages = _extract_rooted(serial, sub, adb)
            if messages is not None:
                logger.info(
                    "[sms/android] Rooted path: extracted %d messages",
                    len(messages),
                )
                return messages
            logger.warning(
                "[sms/android] Rooted path failed, falling back to content "
                "provider"
            )

        messages = _extract_content_provider(serial, adb)
        logger.info(
            "[sms/android] Content provider path: extracted %d messages",
            len(messages),
        )
        return messages

    except Exception:
        logger.exception("[sms/android] Unhandled error during extraction")
        return []


# ---------------------------------------------------------------------------
# Non-rooted path — content provider
# ---------------------------------------------------------------------------

def _extract_content_provider(serial: str, adb: ADBManager) -> list[Message]:
    """Query SMS and MMS via Android content providers (no root required)."""
    messages: list[Message] = []
    messages.extend(_query_sms(serial, adb))
    messages.extend(_query_mms(serial, adb))
    return messages


def _query_sms(serial: str, adb: ADBManager) -> list[Message]:
    """Query the SMS content provider and return Message objects."""
    stdout, stderr, rc = adb.shell(
        serial,
        (
            "content query "
            f"--uri {_URI_SMS} "
            "--projection _id,address,body,date,type,read,thread_id"
        ),
        timeout=120,
    )
    if rc != 0:
        logger.warning("[sms/android] SMS query failed (rc=%d): %s", rc, stderr)
        return []

    rows = _parse_content_rows(stdout)
    results: list[Message] = []

    for row in rows:
        sms_type = _safe_int(row.get("type", "0"))
        if sms_type == 0:
            continue  # unknown type

        is_sent = sms_type in _SENT_TYPES
        address = row.get("address", "").strip() or "unknown"
        read_val = _safe_int(row.get("read", "1"))
        thread_id = _safe_int(row.get("thread_id", "0"))

        results.append(
            Message(
                platform_id=row.get("_id", ""),
                sender=_SELF if is_sent else address,
                recipient=address if is_sent else _SELF,
                body=row.get("body", ""),
                timestamp=_ms_to_dt(row.get("date", "0")),
                is_sent=is_sent,
                service="sms",
                read=bool(read_val),
                sms_type=sms_type,
                thread_id=thread_id,
            )
        )

    logger.debug("[sms/android] Parsed %d SMS rows", len(results))
    return results


def _query_mms(serial: str, adb: ADBManager) -> list[Message]:
    """
    Query the MMS content provider.

    For each MMS message we query its parts to extract the text body and
    identify any binary attachments.  Attachment data is not pulled here —
    we record the MMS part URI so a later stage can fetch it if needed.
    """
    stdout, stderr, rc = adb.shell(
        serial,
        (
            "content query "
            f"--uri {_URI_MMS} "
            "--projection _id,date,msg_box,read"
        ),
        timeout=120,
    )
    if rc != 0:
        logger.warning("[sms/android] MMS query failed (rc=%d): %s", rc, stderr)
        return []

    mms_rows = _parse_content_rows(stdout)
    results: list[Message] = []

    for row in mms_rows:
        msg_box = _safe_int(row.get("msg_box", "0"))
        if msg_box not in (_MMS_BOX_INBOX, _MMS_BOX_SENT):
            continue

        mms_id = row.get("_id", "")
        is_sent = msg_box == _MMS_BOX_SENT
        # MMS date is in seconds (unlike SMS which is in milliseconds)
        date_sec = _safe_int(row.get("date", "0"))
        ts = datetime.utcfromtimestamp(date_sec).replace(tzinfo=timezone.utc)
        read_val = _safe_int(row.get("read", "1"))

        # Fetch address(es) for this MMS thread
        address = _get_mms_address(serial, adb, mms_id, is_sent)

        # Fetch parts — text body and attachments
        body, attachments = _get_mms_parts(serial, adb, mms_id)

        results.append(
            Message(
                platform_id=mms_id,
                sender=_SELF if is_sent else address,
                recipient=address if is_sent else _SELF,
                body=body,
                timestamp=ts,
                is_sent=is_sent,
                attachments=attachments,
                service="mms",
                read=bool(read_val),
            )
        )

    logger.debug("[sms/android] Parsed %d MMS rows", len(results))
    return results


def _get_mms_address(
    serial: str,
    adb: ADBManager,
    mms_id: str,
    is_sent: bool,
) -> str:
    """
    Retrieve the peer phone number for a given MMS message ID.

    MMS addresses are stored in content://mms/<id>/addr.  We look for
    address type 137 (TO) for sent messages and 137/151 (FROM) for received.
    Falls back to the first address found if type matching fails.
    """
    stdout, _, rc = adb.shell(
        serial,
        f"content query --uri content://mms/{mms_id}/addr --projection address,type",
        timeout=30,
    )
    if rc != 0:
        return "unknown"

    addr_rows = _parse_content_rows(stdout)
    if not addr_rows:
        return "unknown"

    # type 137 = TO, 151 = FROM in MMS address types
    _TO = 137
    _FROM = 151
    target_type = _TO if is_sent else _FROM

    for row in addr_rows:
        if _safe_int(row.get("type", "0")) == target_type:
            addr = row.get("address", "").strip()
            if addr and addr != "insert-address-token":
                return addr

    # Fallback: return any non-placeholder address
    for row in addr_rows:
        addr = row.get("address", "").strip()
        if addr and addr != "insert-address-token":
            return addr

    return "unknown"


def _get_mms_parts(
    serial: str,
    adb: ADBManager,
    mms_id: str,
) -> tuple[str, list[MessageAttachment]]:
    """
    Retrieve the text body and attachment list for a given MMS message ID.

    Parts are at content://mms/<id>/part.  We read the text/plain part
    inline; all other parts are recorded as MessageAttachment stubs.
    """
    stdout, _, rc = adb.shell(
        serial,
        (
            f"content query --uri content://mms/{mms_id}/part "
            "--projection _id,ct,name,text"
        ),
        timeout=30,
    )
    if rc != 0:
        return "", []

    part_rows = _parse_content_rows(stdout)
    body_parts: list[str] = []
    attachments: list[MessageAttachment] = []

    for row in part_rows:
        ct = row.get("ct", "")
        if ct == "text/plain":
            text = row.get("text", "")
            if text and text != "null":
                body_parts.append(text)
        elif ct and ct not in ("application/smil",):
            # Record as attachment stub (data not fetched here)
            part_id = row.get("_id", "")
            name = row.get("name", "") or f"mms_part_{part_id}"
            attachments.append(
                MessageAttachment(
                    filename=name,
                    mime_type=ct,
                    data=None,
                    local_path=None,
                )
            )

    return "\n".join(body_parts), attachments


# ---------------------------------------------------------------------------
# Rooted path — direct SQLite access
# ---------------------------------------------------------------------------

def _extract_rooted(
    serial: str,
    sub: Path,
    adb: ADBManager,
) -> list[Message] | None:
    """
    Copy mmssms.db off the device, pull to staging, parse locally.
    Returns None on any failure so the caller can fall back.
    """
    local_db = sub / _LOCAL_DB_NAME

    _, _, rc = adb.shell_root(
        serial,
        f"cp {_REMOTE_DB} {_REMOTE_TMP}",
        timeout=30,
    )
    if rc != 0:
        logger.warning("[sms/android] su cp failed (rc=%d)", rc)
        return None

    adb.shell_root(serial, f"chmod 644 {_REMOTE_TMP}", timeout=10)
    pulled = adb.pull_verified(serial, _REMOTE_TMP, local_db, timeout=120)
    adb.shell(serial, f"rm -f {_REMOTE_TMP}", timeout=10)

    if not pulled or not local_db.exists():
        logger.warning("[sms/android] adb pull of mmssms.db failed")
        return None

    try:
        return _parse_sqlite_messages(local_db)
    except Exception:
        logger.exception("[sms/android] SQLite parse error")
        return None


def _parse_sqlite_messages(db_path: Path) -> list[Message]:
    """Parse mmssms.db directly using sqlite3."""
    messages: list[Message] = []

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row

        # --- SMS table ---
        try:
            cursor = conn.execute(
                "SELECT _id, address, body, date, type, read "
                "FROM sms "
                "WHERE type IN (?, ?)",
                (_TYPE_INBOX, _TYPE_SENT),
            )
            for row in cursor:
                is_sent = row["type"] == _TYPE_SENT
                address = (row["address"] or "unknown").strip()
                messages.append(
                    Message(
                        platform_id=str(row["_id"]),
                        sender=_SELF if is_sent else address,
                        recipient=address if is_sent else _SELF,
                        body=row["body"] or "",
                        timestamp=_ms_to_dt(str(row["date"])),
                        is_sent=is_sent,
                        service="sms",
                        read=bool(row["read"]),
                    )
                )
        except sqlite3.OperationalError:
            logger.warning("[sms/android] sms table unavailable in mmssms.db")

        # --- MMS: pdu + part tables ---
        try:
            pdu_cursor = conn.execute(
                "SELECT _id, date, msg_box, read FROM pdu "
                "WHERE msg_box IN (?, ?)",
                (_MMS_BOX_INBOX, _MMS_BOX_SENT),
            )
            for pdu_row in pdu_cursor:
                mms_id = pdu_row["_id"]
                is_sent = pdu_row["msg_box"] == _MMS_BOX_SENT
                # pdu.date is in seconds
                ts = datetime.utcfromtimestamp(
                    pdu_row["date"] or 0
                ).replace(tzinfo=timezone.utc)

                # Get address
                address = _sqlite_mms_address(conn, mms_id, is_sent)

                # Get parts
                body, attachments = _sqlite_mms_parts(conn, mms_id)

                messages.append(
                    Message(
                        platform_id=str(mms_id),
                        sender=_SELF if is_sent else address,
                        recipient=address if is_sent else _SELF,
                        body=body,
                        timestamp=ts,
                        is_sent=is_sent,
                        attachments=attachments,
                        service="mms",
                        read=bool(pdu_row["read"]),
                    )
                )
        except sqlite3.OperationalError:
            logger.warning("[sms/android] pdu table unavailable in mmssms.db")

    return messages


def _sqlite_mms_address(
    conn: sqlite3.Connection,
    mms_id: int,
    is_sent: bool,
) -> str:
    """Retrieve peer address for an MMS message from the addr table."""
    try:
        _TO = 137
        _FROM = 151
        target_type = _TO if is_sent else _FROM
        cursor = conn.execute(
            "SELECT address, type FROM addr WHERE msg_id = ?",
            (mms_id,),
        )
        rows = cursor.fetchall()
        for row in rows:
            if row["type"] == target_type:
                addr = (row["address"] or "").strip()
                if addr and addr != "insert-address-token":
                    return addr
        for row in rows:
            addr = (row["address"] or "").strip()
            if addr and addr != "insert-address-token":
                return addr
    except sqlite3.OperationalError:
        pass
    return "unknown"


def _sqlite_mms_parts(
    conn: sqlite3.Connection,
    mms_id: int,
) -> tuple[str, list[MessageAttachment]]:
    """Retrieve text body and attachment stubs from the part table."""
    body_parts: list[str] = []
    attachments: list[MessageAttachment] = []
    try:
        cursor = conn.execute(
            "SELECT _id, ct, name, text FROM part WHERE mid = ?",
            (mms_id,),
        )
        for row in cursor:
            ct = row["ct"] or ""
            if ct == "text/plain":
                text = row["text"] or ""
                if text:
                    body_parts.append(text)
            elif ct and ct not in ("application/smil",):
                part_id = row["_id"]
                name = row["name"] or f"mms_part_{part_id}"
                attachments.append(
                    MessageAttachment(
                        filename=name,
                        mime_type=ct,
                        data=None,
                        local_path=None,
                    )
                )
    except sqlite3.OperationalError:
        logger.warning("[sms/android] part table unavailable in mmssms.db")

    return "\n".join(body_parts), attachments


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_int(value: str | None, default: int = 0) -> int:
    """Convert a string to int, returning *default* on failure."""
    try:
        return int(value or default)
    except (ValueError, TypeError):
        return default
