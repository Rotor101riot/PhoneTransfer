"""
extract_whatsapp_ios.py

Extracts WhatsApp messages from an iOS device and returns a list of Message
objects as defined in normalization_schema.py.

WhatsApp on iOS stores messages in an unencrypted SQLite database inside the
app's private container.

Access path (jailbreak required):
    Enumerate /var/mobile/Containers/Data/Application/ via AFC2 to locate
    ChatStorage.sqlite, then pull and parse it.

Non-jailbroken devices are not supported.  WhatsApp's app container is fully
sandboxed and cannot be read from an iTunes/Finder backup without an
Apple-signed entitlement.  Attempting to restore a WhatsApp database from an
iOSbackup dump on a non-jailbroken device would also require writing back into
the app container, which is equally gated behind jailbreak.

Returns a list of Message objects; empty list when the device is not jailbroken
or no access path succeeds.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.normalization_schema import Message, MessageAttachment

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Apple epoch
# ---------------------------------------------------------------------------

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Device paths / backup identifiers
# ---------------------------------------------------------------------------

_APP_CONTAINER_ROOT = "/var/mobile/Containers/Data/Application"
_CHATSTORAGE_RELPATH = "Documents/ChatStorage.sqlite"
_SQLITE_MAGIC = b"SQLite"

_IOSBACKUP_DOMAIN = "AppDomain-net.whatsapp.WhatsApp"
_IOSBACKUP_RELATIVE = "Documents/ChatStorage.sqlite"

# Staging sub-directory
_SUBDIR = "whatsapp_ios"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(
    udid: str,
    staging_dir: Path,
    is_jailbroken: bool = False,
) -> list[Message]:
    """
    Extract WhatsApp messages from an iOS device.

    Parameters
    ----------
    udid:           iOS device UDID.
    staging_dir:    Root staging directory for this transfer session.
    is_jailbroken:  If True, use AFC2 full-filesystem access to locate the DB.

    Returns
    -------
    List of Message objects; empty list on any failure.
    """
    try:
        return _extract_impl(udid, staging_dir, is_jailbroken)
    except Exception:
        logger.exception("[whatsapp/ios] Unhandled error during extraction")
        return []


def _extract_impl(
    udid: str,
    staging_dir: Path,
    is_jailbroken: bool,
) -> list[Message]:
    sub = staging_dir / _SUBDIR
    sub.mkdir(parents=True, exist_ok=True)

    db_path = _obtain_db(udid, sub, is_jailbroken)
    if db_path is None:
        logger.warning(
            "[whatsapp/ios] Could not obtain ChatStorage.sqlite for %s. "
            "Ensure WhatsApp is installed. "
            "Jailbroken devices need AFC2; non-jailbroken devices need an "
            "iTunes/Finder backup.",
            udid,
        )
        return []

    messages = _parse_chatstorage(db_path)
    logger.info("[whatsapp/ios] Extracted %d messages for %s", len(messages), udid)
    return messages


# ---------------------------------------------------------------------------
# Obtain ChatStorage.sqlite
# ---------------------------------------------------------------------------

def _obtain_db(udid: str, sub: Path, is_jailbroken: bool) -> Path | None:
    dest = sub / "ChatStorage.sqlite"

    if not is_jailbroken:
        logger.warning(
            "[whatsapp/ios] WhatsApp extraction requires a jailbroken device. "
            "ChatStorage.sqlite is inside a sandboxed app container that cannot "
            "be accessed without AFC2 (full-filesystem) access. "
            "Skipping extraction for %s.",
            udid,
        )
        return None

    result = _pull_via_afc2(udid, dest)
    if result is None:
        logger.warning(
            "[whatsapp/ios] AFC2 pull of ChatStorage.sqlite failed for %s. "
            "Ensure the AFC2 daemon is running and WhatsApp is installed.",
            udid,
        )
    return result


def _pull_via_afc2(udid: str, dest: Path) -> Path | None:
    """
    Use AFC2 (full filesystem) to locate and pull ChatStorage.sqlite.

    WhatsApp's app container UUID changes on each reinstall, so we enumerate
    all entries under /var/mobile/Containers/Data/Application/ and check each
    one for the presence of Documents/ChatStorage.sqlite.
    """
    try:
        from core.ios_service_broker import IOSServiceBroker  # noqa: F401
        from core.afc2_connector import AFC2Connector
    except ImportError as exc:
        logger.warning("[whatsapp/ios] AFC2 modules not available: %s", exc)
        return None

    try:
        from core.device_connection_cache import get_broker
        broker = get_broker(udid)
        with AFC2Connector(broker) as afc2:
            app_uuids = afc2.list_dir(_APP_CONTAINER_ROOT)
            if not app_uuids:
                logger.debug(
                    "[whatsapp/ios] AFC2 listed 0 entries under %s",
                    _APP_CONTAINER_ROOT,
                )
                return None

            for app_uuid in app_uuids:
                candidate = f"{_APP_CONTAINER_ROOT}/{app_uuid}/{_CHATSTORAGE_RELPATH}"
                data = afc2.read_file(candidate)
                if data and data[:6] == _SQLITE_MAGIC:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                    logger.debug(
                        "[whatsapp/ios] Found ChatStorage.sqlite at %s", candidate
                    )
                    return dest

            logger.warning(
                "[whatsapp/ios] ChatStorage.sqlite not found in any of %d "
                "app containers under %s",
                len(app_uuids),
                _APP_CONTAINER_ROOT,
            )
            return None

    except PermissionError:
        logger.warning(
            "[whatsapp/ios] AFC2 permission error on %s — device must be "
            "jailbroken with the AFC2 package installed",
            udid,
        )
        return None
    except Exception as exc:
        logger.warning("[whatsapp/ios] AFC2 enumeration failed: %s", exc)
        return None


def _pull_via_iosbackup(udid: str, dest: Path) -> Path | None:
    """
    Retrieve ChatStorage.sqlite from an iTunes/Finder backup using iOSbackup.
    This works when the backup is unencrypted (WhatsApp backups are stored
    in plaintext inside the app domain even if the overall backup is not
    encrypted, as long as the backup encryption is disabled).
    """
    try:
        from core.device_connection_cache import get_iosbackup
        dest.parent.mkdir(parents=True, exist_ok=True)
        backup = get_iosbackup(udid)
        info = backup.getFileDecryptedCopy(
            relativePath=_IOSBACKUP_RELATIVE,
            targetName=dest.name,
            targetFolder=str(dest.parent),
        )
        if not info or not dest.exists():
            logger.warning(
                "[whatsapp/ios] iOSbackup returned no data for "
                "%s / %s on %s. "
                "Confirm that a local backup exists and that WhatsApp data "
                "is included.",
                _IOSBACKUP_DOMAIN,
                _IOSBACKUP_RELATIVE,
                udid,
            )
            return None

        logger.debug("[whatsapp/ios] Pulled ChatStorage.sqlite via iOSbackup")
        return dest

    except Exception as exc:
        logger.warning("[whatsapp/ios] iOSbackup pull failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Parse ChatStorage.sqlite
# ---------------------------------------------------------------------------

def _parse_chatstorage(db_path: Path) -> list[Message]:
    """
    Parse the WhatsApp iOS SQLite database.

    Tables used:
      ZWAMESSAGE          — message rows
      ZWACHATSESSION      — maps chat session PK -> contact JID / group subject
      ZWAMEDIAITEM        — media attachments linked to messages
      ZWAPROFILEPUSHNAME  — JID -> display name mapping for all contacts
    """
    messages: list[Message] = []

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row

            # Build chat session map: Z_PK -> JID / group subject
            session_map = _build_session_map(conn)

            # Build push name map: JID -> display name (from ZWAPROFILEPUSHNAME)
            pushname_map = _build_pushname_map(conn)

            # Build media map: ZWAMESSAGE.Z_PK -> list[MessageAttachment]
            media_map = _build_media_map(conn)

            # Query messages
            messages.extend(_query_zwamessage(conn, session_map, media_map, pushname_map))

    except Exception:
        logger.exception("[whatsapp/ios] Failed to open/parse ChatStorage.sqlite")

    return messages


def _build_session_map(conn: sqlite3.Connection) -> dict[int, str]:
    """Return a mapping of ZWACHATSESSION.Z_PK -> contact JID or group subject."""
    result: dict[int, str] = {}
    try:
        # ZCONTACTJID for 1-on-1, ZPARTNERNAME for group display name
        cur = conn.execute(
            "SELECT Z_PK, ZCONTACTJID, ZPARTNERNAME FROM ZWACHATSESSION"
        )
        for row in cur.fetchall():
            pk  = row["Z_PK"]
            jid = row["ZCONTACTJID"] or row["ZPARTNERNAME"] or ""
            result[pk] = jid
    except sqlite3.OperationalError:
        # Older schema: no ZPARTNERNAME
        try:
            cur = conn.execute("SELECT Z_PK, ZCONTACTJID FROM ZWACHATSESSION")
            for row in cur.fetchall():
                result[row["Z_PK"]] = row["ZCONTACTJID"] or ""
        except sqlite3.OperationalError as exc:
            logger.warning("[whatsapp/ios] ZWACHATSESSION table query failed: %s", exc)
    return result


def _build_pushname_map(conn: sqlite3.Connection) -> dict[str, str]:
    """
    Return a mapping of JID -> display name from ZWAPROFILEPUSHNAME.
    This table stores the WhatsApp push names for all contacts the user
    has interacted with, enabling friendly name display for 1:1 chats.
    Falls back to an empty dict if the table doesn't exist (older WhatsApp).
    """
    result: dict[str, str] = {}
    try:
        # Column names vary slightly across WhatsApp versions
        col_info = conn.execute("PRAGMA table_info(ZWAPROFILEPUSHNAME)").fetchall()
        cols = {r[1].upper() for r in col_info}
        jid_col  = "ZJID"      if "ZJID"      in cols else None
        name_col = "ZPUSHNAME" if "ZPUSHNAME" in cols else (
                   "ZNAME"     if "ZNAME"     in cols else None
        )
        if jid_col and name_col:
            cur = conn.execute(
                f"SELECT {jid_col}, {name_col} FROM ZWAPROFILEPUSHNAME "
                f"WHERE {name_col} IS NOT NULL"
            )
            for row in cur.fetchall():
                jid  = row[jid_col]  or ""
                name = row[name_col] or ""
                if jid and name:
                    result[jid] = name
    except sqlite3.OperationalError:
        pass  # table absent in older WhatsApp builds
    return result


def _build_media_map(conn: sqlite3.Connection) -> dict[int, list[MessageAttachment]]:
    """
    Build a map from ZWAMESSAGE.Z_PK -> list[MessageAttachment] using
    ZWAMEDIAITEM (linked via ZMESSAGE foreign key).

    Columns used:
      ZMESSAGE        — FK to ZWAMESSAGE.Z_PK
      ZMEDIALOCALPATH — relative path inside the app container
      ZVCARDNAME      — original filename
      ZMEDIAURL       — remote URL (fallback for filename)
      ZUTIMETYPE      — MIME type hint (e.g. "image/jpeg")
    """
    result: dict[int, list[MessageAttachment]] = {}
    try:
        # Discover available columns
        col_info = conn.execute("PRAGMA table_info(ZWAMEDIAITEM)").fetchall()
        mi_cols  = {r[1].upper() for r in col_info}

        select_parts = ["ZMESSAGE", "ZMEDIALOCALPATH"]
        if "ZVCARDNAME" in mi_cols:
            select_parts.append("ZVCARDNAME")
        if "ZMEDIAURL" in mi_cols:
            select_parts.append("ZMEDIAURL")
        if "ZUTIMETYPE" in mi_cols:
            select_parts.append("ZUTIMETYPE")

        cur = conn.execute(
            f"SELECT {', '.join(select_parts)} FROM ZWAMEDIAITEM "
            "WHERE ZMESSAGE IS NOT NULL"
        )
        for row in cur.fetchall():
            msg_pk = row["ZMESSAGE"]
            local_path_str = row["ZMEDIALOCALPATH"] or ""
            vcardname      = row["ZVCARDNAME"]  if "ZVCARDNAME"  in mi_cols else None
            media_url      = row["ZMEDIAURL"]   if "ZMEDIAURL"   in mi_cols else None
            mime_hint      = row["ZUTIMETYPE"]  if "ZUTIMETYPE"  in mi_cols else None

            # Determine filename
            filename = (
                vcardname
                or (Path(local_path_str).name if local_path_str else None)
                or (media_url.split("/")[-1] if media_url else None)
                or f"attachment_{msg_pk}"
            )

            # Determine MIME type
            mime = _guess_mime(mime_hint, filename)

            local_path = Path(local_path_str) if local_path_str else None

            att = MessageAttachment(
                filename=filename,
                mime_type=mime,
                local_path=local_path,
            )
            result.setdefault(msg_pk, []).append(att)

    except sqlite3.OperationalError as exc:
        logger.debug("[whatsapp/ios] ZWAMEDIAITEM not available: %s", exc)

    return result


def _query_zwamessage(
    conn: sqlite3.Connection,
    session_map: dict[int, str],
    media_map: dict[int, list[MessageAttachment]],
    pushname_map: dict[str, str] | None = None,
) -> list[Message]:
    """Query ZWAMESSAGE and build Message objects."""
    results: list[Message] = []

    # Discover optional columns
    try:
        col_info  = conn.execute("PRAGMA table_info(ZWAMESSAGE)").fetchall()
        wamsg_cols = {r[1].upper() for r in col_info}
    except Exception:
        wamsg_cols = set()

    select_parts = ["Z_PK", "ZTEXT", "ZMESSAGEDATE", "ZISFROMME", "ZCHATSESSION"]
    # ZPUSHNAME = sender display name in group chats (WhatsApp iOS 2.x+)
    if "ZPUSHNAME" in wamsg_cols:
        select_parts.append("ZPUSHNAME")
    # ZFROMJID = sender JID in group messages (newer WhatsApp)
    if "ZFROMJID" in wamsg_cols:
        select_parts.append("ZFROMJID")

    try:
        cur = conn.execute(
            f"SELECT {', '.join(select_parts)} FROM ZWAMESSAGE "
            "ORDER BY ZMESSAGEDATE ASC"
        )
    except sqlite3.OperationalError as exc:
        logger.error(
            "[whatsapp/ios] ZWAMESSAGE query failed: %s. "
            "Schema may differ across WhatsApp versions.",
            exc,
        )
        return results

    has_pushname = "ZPUSHNAME" in wamsg_cols
    has_fromjid  = "ZFROMJID"  in wamsg_cols

    pn_map = pushname_map or {}

    for row in cur.fetchall():
        try:
            msg = _wa_ios_row_to_message(
                row, session_map, media_map, pn_map,
                has_pushname=has_pushname,
                has_fromjid=has_fromjid,
            )
            results.append(msg)
        except Exception as exc:
            logger.debug(
                "[whatsapp/ios] Skipping message row %s: %s",
                row["Z_PK"],
                exc,
            )

    return results


def _wa_ios_row_to_message(
    row: sqlite3.Row,
    session_map: dict[int, str],
    media_map: dict[int, list[MessageAttachment]],
    pushname_map: dict[str, str],
    *,
    has_pushname: bool,
    has_fromjid: bool,
) -> Message:
    pk         = row["Z_PK"]
    body       = row["ZTEXT"] or ""
    ts_raw     = row["ZMESSAGEDATE"]
    is_sent    = bool(row["ZISFROMME"])
    session_pk = row["ZCHATSESSION"] or 0

    timestamp = _apple_ts_to_dt(ts_raw)
    jid = session_map.get(session_pk, "")
    # Try to resolve a friendly name from ZWAPROFILEPUSHNAME for the chat peer
    chat_display = pushname_map.get(jid) or _jid_to_phone(jid)
    _jid_to_phone(jid)

    # Resolve the actual sender for group messages (ZISFROMME=0 in group)
    if not is_sent:
        if has_fromjid and row["ZFROMJID"]:
            from_jid = row["ZFROMJID"]
            # Prefer push name from DB; fall back to ZPUSHNAME on the row, then JID->phone
            sender = (
                pushname_map.get(from_jid)
                or (row["ZPUSHNAME"] if has_pushname and row["ZPUSHNAME"] else None)
                or _jid_to_phone(from_jid)
            )
        elif has_pushname and row["ZPUSHNAME"]:
            sender = row["ZPUSHNAME"]
        else:
            sender = chat_display
    else:
        sender = "self"

    recipient = chat_display if is_sent else "self"
    attachments = media_map.get(pk, [])

    return Message(
        platform_id=str(pk),
        sender=sender,
        recipient=recipient,
        body=body,
        timestamp=timestamp,
        is_sent=is_sent,
        attachments=attachments,
        service="sms",
        read=True,
    )


def _guess_mime(hint: str | None, filename: str | None) -> str:
    """Best-effort MIME type from ZUTIMETYPE hint or filename extension."""
    if hint and "/" in hint:
        return hint
    if filename:
        ext = Path(filename).suffix.lower()
        ext_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif",
            ".mp4": "video/mp4", ".mov": "video/quicktime",
            ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
            ".ogg": "audio/ogg", ".opus": "audio/ogg",
            ".pdf": "application/pdf",
            ".doc": "application/msword",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        if ext in ext_map:
            return ext_map[ext]
    return "application/octet-stream"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apple_ts_to_dt(ts: float | int | None) -> datetime:
    """Convert an Apple epoch timestamp (seconds since 2001-01-01) to UTC datetime."""
    if ts is None or ts == 0:
        return _APPLE_EPOCH
    try:
        return _APPLE_EPOCH + timedelta(seconds=float(ts))
    except (OverflowError, OSError, ValueError):
        return _APPLE_EPOCH


def _jid_to_phone(jid: str) -> str:
    """
    Extract a normalised phone number from a WhatsApp JID.

    Examples:
        "15551234567@s.whatsapp.net" -> "+15551234567"
        "15551234567-1609459200@g.us" -> "+15551234567"  (group — best effort)
        ""                            -> "unknown"
    """
    if not jid:
        return "unknown"
    phone = jid.split("@")[0] if "@" in jid else jid
    phone = phone.split("-")[0]
    if phone and not phone.startswith("+"):
        phone = "+" + phone
    return phone or "unknown"
