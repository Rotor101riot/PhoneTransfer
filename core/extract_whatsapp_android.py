"""
extract_whatsapp_android.py

Extracts WhatsApp messages from an Android device connected via ADB.

WhatsApp stores messages in a SQLCipher-variant encrypted database
(msgstore.db.crypt15 / .crypt14 / .crypt12) on the shared storage.
The decryption key lives in the private app directory and requires root.

Two extraction paths:
- Rooted:     Pull the crypt DB + key, decrypt with wa-crypt-tools,
              parse the resulting SQLite.
- Non-rooted: Pull the crypt DB to staging (cannot decrypt without key);
              log guidance for the user and return [].

Returns a list of Message objects as defined in normalization_schema.py.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.adb_manager import ADBManager
from core.config_loader import get_config
from core.normalization_schema import Message, MessageAttachment

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# wa-crypt-tools — optional; try multiple API versions
# ---------------------------------------------------------------------------

try:
    from wa_crypt_tools.lib.db.db15 import Database15  # type: ignore[import]
except ImportError:
    try:
        from wa_crypt_tools.lib.db.db14 import Database14 as Database15  # type: ignore[import]
    except ImportError:
        Database15 = None  # type: ignore[assignment,misc]

try:
    from wa_crypt_tools.lib.key.keyfactory import KeyFactory  # type: ignore[import]
except ImportError:
    KeyFactory = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Remote paths on the Android device
# ---------------------------------------------------------------------------

_DB_CANDIDATES = [
    "/sdcard/Android/media/com.whatsapp/WhatsApp/Databases/msgstore.db.crypt15",
    "/sdcard/Android/media/com.whatsapp/WhatsApp/Databases/msgstore.db.crypt14",
    "/sdcard/Android/media/com.whatsapp/WhatsApp/Databases/msgstore.db.crypt12",
]
_KEY_REMOTE_SRC = "/data/data/com.whatsapp/files/key"
_KEY_REMOTE_TMP = "/sdcard/wa_key_tmp"

# Staging sub-directory
_SUBDIR = "whatsapp_android"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(
    serial: str,
    staging_dir: Path,
    is_rooted: bool = False,
) -> list[Message]:
    """
    Extract WhatsApp messages from an Android device.

    Parameters
    ----------
    serial:      ADB device serial string.
    staging_dir: Root staging directory for this transfer session.
    is_rooted:   If True, attempt to pull the decryption key (requires root).

    Returns
    -------
    List of Message objects; empty list when decryption is not possible.
    """
    try:
        return _extract_impl(serial, staging_dir, is_rooted)
    except Exception:
        logger.exception("[whatsapp/android] Unhandled error during extraction")
        return []


def _extract_impl(
    serial: str,
    staging_dir: Path,
    is_rooted: bool,
) -> list[Message]:
    sub = staging_dir / _SUBDIR
    sub.mkdir(parents=True, exist_ok=True)

    adb = ADBManager(get_config())

    # ------------------------------------------------------------------
    # Step 1: Pull the encrypted database (accessible without root)
    # ------------------------------------------------------------------
    crypt_path = _pull_crypt_db(serial, sub, adb)
    if crypt_path is None:
        logger.warning(
            "[whatsapp/android] Could not pull WhatsApp crypt DB from %s. "
            "Ensure WhatsApp is installed and the database exists at "
            "/sdcard/Android/media/com.whatsapp/WhatsApp/Databases/",
            serial,
        )
        return []

    # ------------------------------------------------------------------
    # Step 2: Obtain the decryption key (root required)
    # ------------------------------------------------------------------
    if not is_rooted:
        logger.info(
            "[whatsapp/android] WhatsApp key requires root access. "
            "Encrypted DB pulled to staging for future decryption: %s",
            crypt_path,
        )
        return []

    key_path = sub / "key"
    if not _pull_key(serial, key_path, adb):
        logger.warning(
            "[whatsapp/android] Failed to pull WhatsApp key even with root. "
            "Encrypted DB is at %s — supply the key manually to decrypt later.",
            crypt_path,
        )
        return []

    # ------------------------------------------------------------------
    # Step 3: Decrypt the database
    # ------------------------------------------------------------------
    decrypted_path = sub / "msgstore.db"
    if not _decrypt_db(crypt_path, key_path, decrypted_path):
        logger.error(
            "[whatsapp/android] Decryption failed. Encrypted DB staged at %s.",
            crypt_path,
        )
        return []

    # ------------------------------------------------------------------
    # Step 4: Parse the decrypted SQLite
    # ------------------------------------------------------------------
    messages = _parse_msgstore(decrypted_path)
    logger.info("[whatsapp/android] Parsed %d messages", len(messages))

    # ------------------------------------------------------------------
    # Step 5: Pull media files referenced in attachments
    # ------------------------------------------------------------------
    messages = _pull_media_files(serial, messages, sub, adb)
    logger.info("[whatsapp/android] Extracted %d messages (with media)", len(messages))
    return messages


# ---------------------------------------------------------------------------
# Pull crypt DB
# ---------------------------------------------------------------------------

def _pull_crypt_db(serial: str, sub: Path, adb: ADBManager) -> Path | None:
    """
    Attempt to pull msgstore.db.crypt15 (or crypt14 / crypt12 as fallback).
    Returns the local path on success, None if none of the candidates exist.
    """
    for remote_path in _DB_CANDIDATES:
        ext = remote_path.rsplit(".", 1)[-1]          # e.g. "crypt15"
        local_path = sub / f"msgstore.db.{ext}"
        ok = adb.pull(serial, remote_path, local_path, timeout=180)
        if ok and local_path.exists() and local_path.stat().st_size > 0:
            logger.debug(
                "[whatsapp/android] Pulled crypt DB from %s -> %s",
                remote_path,
                local_path,
            )
            return local_path

    return None


# ---------------------------------------------------------------------------
# Pull decryption key
# ---------------------------------------------------------------------------

def _pull_key(serial: str, local_key: Path, adb: ADBManager) -> bool:
    """
    Copy the WhatsApp key from the private app directory to /sdcard/, then
    pull it.  Cleans up the temporary copy afterwards.  Requires root.
    """
    _, _, rc = adb.shell_root(
        serial,
        f"cp {_KEY_REMOTE_SRC} {_KEY_REMOTE_TMP}",
        timeout=20,
    )
    if rc != 0:
        logger.warning(
            "[whatsapp/android] su cp of WhatsApp key failed (rc=%d). "
            "The key path is %s — it may not exist if WhatsApp is not installed "
            "or the backup key mechanism changed.",
            rc,
            _KEY_REMOTE_SRC,
        )
        return False

    # Make world-readable so ADB pull can grab it
    adb.shell_root(serial, f"chmod 644 {_KEY_REMOTE_TMP}", timeout=10)

    ok = adb.pull(serial, _KEY_REMOTE_TMP, local_key, timeout=30)
    # Clean up regardless of pull success
    adb.shell(serial, f"rm -f {_KEY_REMOTE_TMP}", timeout=10)

    if not ok or not local_key.exists():
        logger.warning("[whatsapp/android] Pull of WhatsApp key file failed")
        return False

    logger.debug("[whatsapp/android] Key pulled to %s", local_key)
    return True


# ---------------------------------------------------------------------------
# Decrypt with wa-crypt-tools
# ---------------------------------------------------------------------------

def _decrypt_db(crypt_path: Path, key_path: Path, out_path: Path) -> bool:
    """
    Decrypt *crypt_path* into *out_path* using wa-crypt-tools.
    Returns True on success.
    """
    if Database15 is None or KeyFactory is None:
        logger.error(
            "[whatsapp/android] wa_crypt_tools is not installed. "
            "Install it with: pip install wa-crypt-tools"
        )
        return False

    try:
        key = KeyFactory.from_file(str(key_path))
        db_obj = Database15(key, str(crypt_path))
        db_obj.decrypt(str(out_path))
        logger.debug(
            "[whatsapp/android] Decrypted %s -> %s", crypt_path.name, out_path.name
        )
        return True
    except Exception as exc:
        logger.error("[whatsapp/android] WhatsApp decrypt failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Parse decrypted msgstore.db
# ---------------------------------------------------------------------------

def _parse_msgstore(db_path: Path) -> list[Message]:
    """
    Parse the decrypted WhatsApp SQLite database and return Message objects.

    Tables used:
      messages        — core message data
      message_media   — media attachment metadata (joined on _id)
    """
    messages: list[Message] = []

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            messages.extend(_query_messages(conn))
    except Exception:
        logger.exception("[whatsapp/android] Failed to open/parse msgstore.db")

    return messages


def _query_messages(conn: sqlite3.Connection) -> list[Message]:
    """Query the messages table and join with message_media."""
    results: list[Message] = []

    try:
        cur = conn.execute(
            """
            SELECT
                m._id,
                m.key_remote_jid,
                m.from_me,
                m.timestamp,
                m.data,
                m.received_timestamp,
                m.status
            FROM messages AS m
            ORDER BY m.timestamp ASC
            """
        )
    except sqlite3.OperationalError as exc:
        logger.error(
            "[whatsapp/android] messages table query failed: %s. "
            "The schema may differ across WhatsApp versions.",
            exc,
        )
        return results

    # Build media map: message _id -> list[MessageAttachment]
    media_map = _build_media_map(conn)

    for row in cur.fetchall():
        try:
            msg = _row_to_message(row, media_map)
            results.append(msg)
        except Exception as exc:
            logger.debug(
                "[whatsapp/android] Skipping message row %s: %s",
                row["_id"],
                exc,
            )

    return results


def _build_media_map(conn: sqlite3.Connection) -> dict[int, list[MessageAttachment]]:
    """
    Query message_media and return a mapping of message _id -> attachments.
    Gracefully handles databases where this table does not exist.
    """
    result: dict[int, list[MessageAttachment]] = {}
    try:
        cur = conn.execute(
            """
            SELECT
                message_row_id,
                file_path,
                mime_type,
                file_size
            FROM message_media
            """
        )
        for row in cur.fetchall():
            mid = row["message_row_id"]
            file_path = row["file_path"] or ""
            mime = row["mime_type"] or "application/octet-stream"
            filename = Path(file_path).name if file_path else f"media_{mid}"
            att = MessageAttachment(
                filename=filename,
                mime_type=mime,
                data=None,
                local_path=Path(file_path) if file_path else None,
            )
            result.setdefault(mid, []).append(att)
    except sqlite3.OperationalError:
        # message_media may not exist in all schema versions — not fatal
        logger.debug(
            "[whatsapp/android] message_media table not found; "
            "media attachments will not be included"
        )

    return result


def _row_to_message(
    row: sqlite3.Row,
    media_map: dict[int, list[MessageAttachment]],
) -> Message:
    row_id = row["_id"]
    jid = row["key_remote_jid"] or ""
    from_me = bool(row["from_me"])
    ts_ms = row["timestamp"] or 0
    body = row["data"] or ""

    timestamp = datetime.utcfromtimestamp(ts_ms / 1000).replace(tzinfo=timezone.utc)
    phone = _jid_to_phone(jid)

    sender = "self" if from_me else phone
    recipient = phone if from_me else "self"

    attachments = media_map.get(row_id, [])

    return Message(
        platform_id=str(row_id),
        sender=sender,
        recipient=recipient,
        body=body,
        timestamp=timestamp,
        is_sent=from_me,
        attachments=attachments,
        # WhatsApp is not SMS, but "sms" is the closest fit in the schema
        service="sms",
        read=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pull_media_files(
    serial: str,
    messages: list[Message],
    sub: Path,
    adb: ADBManager,
) -> list[Message]:
    """
    Pull WhatsApp media files from external storage to the staging directory.

    WhatsApp stores most media at paths like:
      /storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media/...
    These are on external storage and accessible via ADB without root.

    Attachments whose device paths cannot be pulled (missing or in private
    storage) retain local_path=None — the message text is still included.
    """
    media_dir = sub / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    seen: dict[str, Path] = {}   # device path → local path (cache)

    for msg in messages:
        for att in msg.attachments:
            if att.local_path is None:
                continue

            device_path = str(att.local_path)
            # Only pull if it's on accessible external storage
            if not (device_path.startswith("/storage/") or
                    device_path.startswith("/sdcard/")):
                continue

            if device_path in seen:
                att.local_path = seen[device_path]
                continue

            filename  = Path(device_path).name
            local_dst = media_dir / filename
            # Deduplicate filenames
            counter = 1
            while local_dst.exists():
                stem, suffix = Path(filename).stem, Path(filename).suffix
                local_dst = media_dir / f"{stem}_{counter}{suffix}"
                counter += 1

            ok = adb.pull(serial, device_path, local_dst, timeout=60)
            if ok and local_dst.exists() and local_dst.stat().st_size > 0:
                att.local_path = local_dst
                seen[device_path] = local_dst
                logger.debug("[whatsapp/android] Pulled media: %s", filename)
            else:
                att.local_path = None
                seen[device_path] = None
                logger.debug("[whatsapp/android] Could not pull media: %s", device_path)

    return messages


def _jid_to_phone(jid: str) -> str:
    """
    Extract a phone number from a WhatsApp JID.

    Examples:
        "15551234567@s.whatsapp.net" -> "+15551234567"
        "15551234567-1609459200@g.us" -> "+15551234567"  (group — best effort)
        ""                            -> "unknown"
    """
    if not jid:
        return "unknown"
    phone = jid.split("@")[0] if "@" in jid else jid
    # Strip group suffix (number-timestamp)
    phone = phone.split("-")[0]
    if phone and not phone.startswith("+"):
        phone = "+" + phone
    return phone or "unknown"
