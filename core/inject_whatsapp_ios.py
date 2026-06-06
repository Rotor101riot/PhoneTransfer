"""
inject_whatsapp_ios.py

Injects WhatsApp messages into an iOS device.

Non-jailbroken: not possible.  WhatsApp's ChatStorage.sqlite lives inside the
app's private container and cannot be written through AFC or MobileSync.
Messages are exported to staging as JSON.

Jailbroken (via AFC2):
  1. Enumerate /var/mobile/Containers/Data/Application/ to locate the
     WhatsApp container UUID (the same scan used by the iOS extractor).
  2. Pull ChatStorage.sqlite to staging.
  3. Ensure a ZWACHATSESSION row exists for each unique peer JID.
  4. Insert ZWAMESSAGE rows for each Message object.
  5. Push the modified DB back via AFC2.write_file().
  6. Log a user-facing instruction to force-quit and reopen WhatsApp
     (required for WhatsApp to reload the CoreData persistent store).

Returns the count of messages successfully written to the device database.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.normalization_schema import Message

logger = logging.getLogger(__name__)

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

_APP_CONTAINER_ROOT = "/var/mobile/Containers/Data/Application"
_CHATSTORAGE_RELPATH = "Documents/ChatStorage.sqlite"
_SQLITE_MAGIC = b"SQLite"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def inject(
    device_id: str,
    items: list[Message],
    staging_dir: Path,
    is_privileged: bool,
) -> int:
    staging_dir.mkdir(parents=True, exist_ok=True)

    if not is_privileged:
        logger.warning(
            "[whatsapp/ios] Injecting WhatsApp messages into a non-jailbroken "
            "iOS device is not supported.  WhatsApp's ChatStorage.sqlite resides "
            "in the app's private container and cannot be written via AFC. "
            "Messages will be exported to %s/whatsapp_ios_export.json for reference.",
            staging_dir,
        )
        _export_json(items, staging_dir)
        return 0

    if not items:
        logger.info("[whatsapp/ios] No messages to inject for %s", device_id)
        return 0

    return _inject_jailbroken(device_id, items, staging_dir)


# ---------------------------------------------------------------------------
# Jailbroken path
# ---------------------------------------------------------------------------

def _inject_jailbroken(
    device_id: str,
    items: list[Message],
    staging_dir: Path,
) -> int:
    try:
        from core.afc2_connector import AFC2Connector
    except ImportError:
        logger.error("[whatsapp/ios] AFC2Connector not available")
        return 0

    local_db = staging_dir / "ChatStorage_inject.sqlite"

    # ------------------------------------------------------------------
    # Step 1: Find ChatStorage.sqlite
    # ------------------------------------------------------------------
    db_remote_path = _find_chatstorage(device_id)
    if db_remote_path is None:
        logger.error(
            "[whatsapp/ios] WhatsApp ChatStorage.sqlite not found on %s. "
            "Ensure WhatsApp is installed and has been opened at least once.",
            device_id,
        )
        return 0

    # ------------------------------------------------------------------
    # Step 2: Pull the database
    # ------------------------------------------------------------------
    try:
        with AFC2Connector(device_id) as afc2:
            data = afc2.read_file(db_remote_path)
        if not data or data[:6] != _SQLITE_MAGIC:
            logger.error("[whatsapp/ios] Pulled file is not a valid SQLite DB")
            return 0
        local_db.write_bytes(data)
    except Exception as exc:
        logger.error("[whatsapp/ios] Failed to pull ChatStorage.sqlite: %s", exc)
        return 0

    # ------------------------------------------------------------------
    # Step 3: Insert messages
    # ------------------------------------------------------------------
    inserted = _insert_messages(local_db, items)
    if inserted == 0:
        logger.warning("[whatsapp/ios] No messages were inserted; skipping push")
        return 0

    # ------------------------------------------------------------------
    # Step 4: Push back
    # ------------------------------------------------------------------
    try:
        with AFC2Connector(device_id) as afc2:
            afc2.write_file(db_remote_path, local_db.read_bytes())
    except Exception as exc:
        logger.error("[whatsapp/ios] Failed to write ChatStorage.sqlite back: %s", exc)
        return 0

    logger.info(
        "[whatsapp/ios] Injected %d message(s) into %s. "
        "Force-quit WhatsApp on the device and reopen it to see the new messages.",
        inserted, device_id,
    )
    return inserted


def _find_chatstorage(device_id: str) -> str | None:
    """
    Enumerate /var/mobile/Containers/Data/Application/ via AFC2 and return
    the full path to WhatsApp's ChatStorage.sqlite, or None if not found.
    """
    try:
        from core.afc2_connector import AFC2Connector
    except ImportError:
        return None

    try:
        with AFC2Connector(device_id) as afc2:
            app_uuids = afc2.list_dir(_APP_CONTAINER_ROOT)
            if not app_uuids:
                return None

            for app_uuid in app_uuids:
                candidate = f"{_APP_CONTAINER_ROOT}/{app_uuid}/{_CHATSTORAGE_RELPATH}"
                try:
                    # Read first 6 bytes only to check magic
                    data = afc2.read_file(candidate)
                    if data and data[:6] == _SQLITE_MAGIC:
                        logger.debug("[whatsapp/ios] Found ChatStorage at %s", candidate)
                        return candidate
                except Exception:
                    continue
    except Exception as exc:
        logger.warning("[whatsapp/ios] AFC2 container scan failed: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Database insertion
# ---------------------------------------------------------------------------

def _insert_messages(db_path: Path, messages: list[Message]) -> int:
    """
    Insert Message objects into ChatStorage.sqlite.

    CoreData tables written:
      ZWACHATSESSION  — one row per unique peer JID
      ZWAMESSAGE      — one row per message
    """
    inserted = 0
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row

            # Discover CoreData entity type IDs from Z_PRIMARYKEY
            ent_session, ent_message = _get_entity_ids(conn)

            # Build session map: jid → Z_PK in ZWACHATSESSION
            session_map = _build_session_map(conn)

            # Group messages by peer JID and ensure sessions exist
            for msg in messages:
                jid = _phone_to_jid(msg.recipient if msg.is_sent else msg.sender)
                if jid not in session_map:
                    pk = _create_session(conn, jid, ent_session)
                    if pk is not None:
                        session_map[jid] = pk

            # Insert messages
            for msg in messages:
                jid = _phone_to_jid(msg.recipient if msg.is_sent else msg.sender)
                session_pk = session_map.get(jid)
                if session_pk is None:
                    logger.debug("[whatsapp/ios] No session for JID %s; skipping", jid)
                    continue
                try:
                    _insert_one_message(conn, msg, session_pk, ent_message)
                    inserted += 1
                except Exception as exc:
                    logger.debug("[whatsapp/ios] Skipping message: %s", exc)

            conn.commit()

    except Exception:
        logger.exception("[whatsapp/ios] Failed to open/modify ChatStorage.sqlite")

    return inserted


def _get_entity_ids(conn: sqlite3.Connection) -> tuple[int, int]:
    """
    Return (session_ent, message_ent) from Z_PRIMARYKEY.
    Falls back to common defaults (3, 11) if the table is missing or
    WAChatSession/WAMessage are not listed.
    """
    session_ent = 3
    message_ent = 11
    try:
        cur = conn.execute("SELECT Z_ENT, Z_NAME FROM Z_PRIMARYKEY")
        for row in cur.fetchall():
            name = (row["Z_NAME"] or "").upper()
            if "CHATSESSION" in name or "SESSION" in name:
                session_ent = row["Z_ENT"]
            elif "MESSAGE" in name and "MEDIA" not in name:
                message_ent = row["Z_ENT"]
    except sqlite3.OperationalError:
        pass
    return session_ent, message_ent


def _build_session_map(conn: sqlite3.Connection) -> dict[str, int]:
    """Return {jid: Z_PK} for all existing ZWACHATSESSION rows."""
    result: dict[str, int] = {}
    try:
        cur = conn.execute("SELECT Z_PK, ZCONTACTJID FROM ZWACHATSESSION")
        for row in cur.fetchall():
            if row["ZCONTACTJID"]:
                result[row["ZCONTACTJID"]] = row["Z_PK"]
    except sqlite3.OperationalError:
        pass
    return result


def _create_session(
    conn: sqlite3.Connection,
    jid: str,
    ent_session: int,
) -> int | None:
    """
    Insert a minimal ZWACHATSESSION row and return its Z_PK.

    We only set the columns that WhatsApp requires for a session to load.
    Unknown / nullable columns are left NULL.
    """
    try:
        # Discover actual columns to avoid inserting non-existent fields
        col_info = conn.execute("PRAGMA table_info(ZWACHATSESSION)").fetchall()
        cols = {r[1].upper() for r in col_info}

        parts: list[tuple[str, object]] = [
            ("Z_ENT",         ent_session),
            ("Z_OPT",         1),
            ("ZCONTACTJID",   jid),
            ("ZSESSIONTYPE",  0),     # 0 = individual
            ("ZUNREADCOUNT",  0),
            ("ZHIDDEN",       0),
            ("ZARCHIVED",     0),
            ("ZLASTMESSAGEDATE", _now_apple()),
        ]
        # ZPARTNERNAME: display name derived from JID phone number
        if "ZPARTNERNAME" in cols:
            parts.append(("ZPARTNERNAME", jid.split("@")[0]))

        valid = [(col, val) for col, val in parts if col in cols or col in ("Z_ENT", "Z_OPT")]
        col_names = ", ".join(col for col, _ in valid)
        placeholders = ", ".join("?" for _ in valid)
        values = [val for _, val in valid]

        c = conn.execute(
            f"INSERT INTO ZWACHATSESSION ({col_names}) VALUES ({placeholders})",
            values,
        )
        return c.lastrowid
    except Exception as exc:
        logger.warning("[whatsapp/ios] Failed to create session for %s: %s", jid, exc)
        return None


def _insert_one_message(
    conn: sqlite3.Connection,
    msg: Message,
    session_pk: int,
    ent_message: int,
) -> None:
    """
    Insert a single Message row into ZWAMESSAGE.

    Columns mapping:
      ZCHATSESSION → session_pk
      ZTEXT        → msg.body
      ZMESSAGEDATE → Apple epoch timestamp
      ZISFROMME    → 1 if msg.is_sent else 0
      ZMESSAGETYPE → 0 (text)
      ZMESSAGESTATUS → 4 (sent+read) or 5 (received+read)
    """
    apple_ts  = _unix_to_apple(msg.timestamp)
    is_from_me = 1 if msg.is_sent else 0
    status     = 4 if msg.is_sent else 5  # 4=sent, 5=received

    # Discover available columns
    col_info = conn.execute("PRAGMA table_info(ZWAMESSAGE)").fetchall()
    cols = {r[1].upper() for r in col_info}

    parts: list[tuple[str, object]] = [
        ("Z_ENT",          ent_message),
        ("Z_OPT",          1),
        ("ZCHATSESSION",   session_pk),
        ("ZISFROMME",      is_from_me),
        ("ZMESSAGEDATE",   apple_ts),
        ("ZMESSAGETYPE",   0),
        ("ZMESSAGESTATUS", status),
        ("ZSTARRED",       0),
        ("ZSPOTLIGHTSTATUS", 0),
    ]
    if "ZTEXT" in cols:
        parts.append(("ZTEXT", msg.body))
    if "ZISFROMME" not in cols and "ZFROMME" in cols:
        parts = [(("ZFROMME" if c == "ZISFROMME" else c), v) for c, v in parts]

    col_names    = ", ".join(col for col, _ in parts if col in cols or col.startswith("Z_"))
    placeholders = ", ".join("?" for col, _ in parts if col in cols or col.startswith("Z_"))
    values       = [val for col, val in parts if col in cols or col.startswith("Z_")]

    conn.execute(
        f"INSERT INTO ZWAMESSAGE ({col_names}) VALUES ({placeholders})",
        values,
    )


# ---------------------------------------------------------------------------
# JSON export (fallback)
# ---------------------------------------------------------------------------

def _export_json(items: list[Message], staging_dir: Path) -> None:
    if not items:
        return
    out = staging_dir / "whatsapp_ios_export.json"
    try:
        records = [
            {
                "platform_id": msg.platform_id,
                "sender":      msg.sender,
                "recipient":   msg.recipient,
                "body":        msg.body,
                "timestamp":   msg.timestamp.isoformat() if msg.timestamp else None,
                "is_sent":     msg.is_sent,
                "service":     msg.service,
                "read":        msg.read,
            }
            for msg in items
        ]
        out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("[whatsapp/ios] Exported %d messages to %s", len(items), out)
    except Exception as exc:
        logger.warning("[whatsapp/ios] JSON export failed: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _phone_to_jid(phone: str | None) -> str:
    if not phone or phone == "self" or phone == "unknown":
        return "unknown@s.whatsapp.net"
    digits = "".join(c for c in phone if c.isdigit())
    return f"{digits}@s.whatsapp.net" if digits else "unknown@s.whatsapp.net"


def _unix_to_apple(dt: datetime | None) -> float:
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - _APPLE_EPOCH).total_seconds()


def _now_apple() -> float:
    return (datetime.now(timezone.utc) - _APPLE_EPOCH).total_seconds()
