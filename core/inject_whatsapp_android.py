from __future__ import annotations

import logging
import sqlite3
import subprocess
import uuid
from pathlib import Path

from core.config_loader import get_config
from core.normalization_schema import Message

logger = logging.getLogger(__name__)

_WA_PACKAGE = "com.whatsapp"
_WA_DB_DEVICE = f"/data/data/{_WA_PACKAGE}/databases/msgstore.db"
_SDCARD_PULL = "/sdcard/PT_msgstore_tmp.db"
_SDCARD_PUSH = "/sdcard/PT_msgstore_inject.db"


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _phone_to_jid(phone: str | None) -> str:
    """Convert a phone number to a WhatsApp JID (e.g., '15551234567@s.whatsapp.net')."""
    if not phone:
        return "unknown@s.whatsapp.net"
    # Strip non-digit chars except leading +
    digits = "".join(c for c in phone if c.isdigit())
    return f"{digits}@s.whatsapp.net"


def _is_whatsapp_installed(adb: str, device_id: str) -> bool:
    try:
        result = _run(
            [adb, "-s", device_id, "shell", "pm", "list", "packages"]
        )
        return _WA_PACKAGE in result.stdout
    except Exception as exc:
        logger.warning("Could not check WhatsApp installation: %s", exc)
        return False


def _get_whatsapp_uid(adb: str, device_id: str) -> str | None:
    """Return the numeric UID of the WhatsApp app process."""
    try:
        result = _run(
            [adb, "-s", device_id, "shell", "su", "-c",
             f"stat -c %u /data/data/{_WA_PACKAGE}"]
        )
        uid = result.stdout.strip()
        if uid.isdigit():
            return uid
    except Exception as exc:
        logger.warning("Could not determine WhatsApp UID: %s", exc)
    return None


def _insert_messages(db_path: Path, messages: list[Message]) -> int:
    """Insert messages into a local copy of WhatsApp msgstore.db."""
    inserted = 0
    try:
        con = sqlite3.connect(str(db_path))
        cur = con.cursor()

        # Verify the messages table exists
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        )
        if not cur.fetchone():
            logger.error("'messages' table not found in msgstore.db at %s", db_path)
            con.close()
            return 0

        for msg in messages:
            try:
                key_id = str(uuid.uuid4())
                key_from_me = 1 if msg.is_sent else 0
                jid = _phone_to_jid(
                    msg.recipient if msg.is_sent else msg.sender
                )
                timestamp_ms = (
                    int(msg.timestamp.timestamp() * 1000) if msg.timestamp else 0
                )
                body = msg.body or ""

                cur.execute(
                    """
                    INSERT INTO messages (
                        key_id,
                        key_remote_jid,
                        key_from_me,
                        status,
                        data,
                        timestamp,
                        media_url,
                        media_mime_type,
                        media_size,
                        media_name,
                        latitude,
                        longitude,
                        thumb_image,
                        remote_resource,
                        received_timestamp,
                        send_timestamp,
                        receipt_sent_timestamp,
                        receipt_server_timestamp,
                        receipt_device_timestamp,
                        recipient_count,
                        participant_hash,
                        starred,
                        quoted_row_id,
                        mentioned_jids,
                        multicast_id,
                        edit_version,
                        media_enc_hash,
                        payment_transaction_id,
                        forwarded,
                        preview_url
                    ) VALUES (
                        ?, ?, ?, 0, ?, ?,
                        NULL, NULL, 0, NULL,
                        0.0, 0.0, NULL, NULL,
                        0, 0, 0, 0, 0,
                        0, NULL, 0, 0, NULL,
                        NULL, 0, NULL, NULL, 0, 0
                    )
                    """,
                    (key_id, jid, key_from_me, body, timestamp_ms),
                )
                inserted += 1
            except Exception as exc:
                logger.warning(
                    "Failed to insert message key_id=%s: %s", key_id, exc
                )

        con.commit()
        con.close()
        logger.info("Inserted %d message(s) into local DB copy.", inserted)
    except Exception as exc:
        logger.error("Failed to open/modify local msgstore.db: %s", exc)
    return inserted


def inject(
    device_id: str, items: list[Message], staging_dir: Path, is_privileged: bool
) -> int:
    """Inject WhatsApp messages into Android.

    Requires root. Without root, logs the limitation and returns 0.

    Rooted procedure:
      1. Verify WhatsApp is installed.
      2. Force-stop WhatsApp.
      3. Pull msgstore.db to staging.
      4. Insert new messages into the local copy via SQLite.
      5. Push modified DB back and restore ownership.
      6. Start WhatsApp.

    Args:
        device_id: ADB serial number.
        items: Message objects to inject.
        staging_dir: Local directory for temporary files.
        is_privileged: True if root access is available.

    Returns:
        Number of messages successfully inserted, or 0 on failure/no-op.
    """
    if not items:
        logger.info("No WhatsApp messages to inject for device %s.", device_id)
        return 0

    if not is_privileged:
        logger.warning(
            "WhatsApp message injection into Android requires root. "
            "WhatsApp's database is sandboxed and cannot be written without root. "
            "Consider using WhatsApp's own cloud backup/restore feature instead. "
            "Device %s is not rooted — skipping injection.",
            device_id,
        )
        return 0

    staging_dir.mkdir(parents=True, exist_ok=True)
    cfg = get_config()
    adb = cfg.adb_exe
    local_db = staging_dir / "msgstore_inject.db"

    # 1. Check WhatsApp installation
    if not _is_whatsapp_installed(adb, device_id):
        logger.error(
            "WhatsApp (%s) is not installed on device %s.", _WA_PACKAGE, device_id
        )
        return 0

    # 2. Force-stop WhatsApp
    try:
        _run([adb, "-s", device_id, "shell", "su", "-c",
              f"am force-stop {_WA_PACKAGE}"])
        logger.debug("Force-stopped WhatsApp on device %s.", device_id)
    except Exception as exc:
        logger.warning("Could not force-stop WhatsApp: %s", exc)

    # 3. Pull msgstore.db
    try:
        cp_result = _run(
            [adb, "-s", device_id, "shell", "su", "-c",
             f"cp {_WA_DB_DEVICE} {_SDCARD_PULL} && chmod 644 {_SDCARD_PULL}"]
        )
        if cp_result.returncode != 0:
            logger.error(
                "Root cp of msgstore.db failed for device %s: %s",
                device_id,
                cp_result.stderr.strip(),
            )
            return 0
        pull_result = _run(
            [adb, "-s", device_id, "pull", _SDCARD_PULL, str(local_db)]
        )
        if pull_result.returncode != 0:
            logger.error(
                "adb pull of msgstore.db failed for device %s: %s",
                device_id,
                pull_result.stderr.strip(),
            )
            return 0
        logger.info("Pulled msgstore.db to %s.", local_db)
    except Exception as exc:
        logger.error("Exception pulling msgstore.db from device %s: %s", device_id, exc)
        return 0

    # 4. Insert messages into local copy
    inserted = _insert_messages(local_db, items)
    if inserted == 0:
        logger.warning("No messages were inserted into local DB; aborting push.")
        return 0

    # 5. Push modified DB back
    try:
        wa_uid = _get_whatsapp_uid(adb, device_id)
        push_result = _run(
            [adb, "-s", device_id, "push", str(local_db), _SDCARD_PUSH]
        )
        if push_result.returncode != 0:
            logger.error(
                "adb push of modified msgstore.db failed for device %s: %s",
                device_id,
                push_result.stderr.strip(),
            )
            return 0

        chown_cmd = ""
        if wa_uid:
            chown_cmd = f" && chown {wa_uid}:{wa_uid} {_WA_DB_DEVICE}"

        cp_back = _run(
            [
                adb, "-s", device_id, "shell", "su", "-c",
                f"cp {_SDCARD_PUSH} {_WA_DB_DEVICE}"
                f" && chmod 660 {_WA_DB_DEVICE}"
                f"{chown_cmd}",
            ]
        )
        if cp_back.returncode != 0:
            logger.error(
                "Root cp-back of msgstore.db failed for device %s: %s",
                device_id,
                cp_back.stderr.strip(),
            )
            return 0
        logger.info(
            "Pushed modified msgstore.db back to device %s.", device_id
        )
    except Exception as exc:
        logger.error(
            "Exception pushing msgstore.db to device %s: %s", device_id, exc
        )
        return 0

    # 6. Start WhatsApp
    try:
        _run(
            [adb, "-s", device_id, "shell", "su", "-c",
             f"monkey -p {_WA_PACKAGE} 1"],
            timeout=15,
        )
        logger.debug("Started WhatsApp on device %s.", device_id)
    except Exception as exc:
        logger.warning("Could not start WhatsApp after injection: %s", exc)

    logger.info(
        "Successfully injected %d WhatsApp message(s) into device %s.",
        inserted,
        device_id,
    )
    return inserted
