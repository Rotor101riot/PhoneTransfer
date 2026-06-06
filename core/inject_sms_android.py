"""
inject_sms_android.py

Injects Message records (SMS/MMS) into an Android device connected via USB/ADB.

Strategy
--------
Android restricts third-party SMS database access on modern releases.  Two
injection paths are attempted in order:

1.  Content-provider insert (``adb shell content insert --uri content://sms/…``)
    Works reliably on Android ≤ 5.  On Android 6–9 it may work depending on
    vendor.  On Android 10+ it typically fails with a SecurityException; that
    failure is logged as a warning rather than an error.

2.  Rooted path — direct SQLite insert into mmssms.db via ``sqlite3``.
    Only attempted when ``is_rooted=True`` and the content-provider insert
    returned a non-zero exit code for a given message.

Messages with ``service == "imessage"`` are skipped — iMessage identities
cannot be recreated on Android.

Return value: count of messages successfully injected by either path.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from core.adb_manager import ADBManager
from core.config_loader import get_config
from core.normalization_schema import Message

logger = logging.getLogger(__name__)

_DEVICE_DIR = "/sdcard/PhoneTransfer"
_MMSSMS_DB = "/data/data/com.android.providers.telephony/databases/mmssms.db"

# Android SMS type constants
_SMS_TYPE_INBOX = 1   # received
_SMS_TYPE_SENT  = 2   # sent

# Map is_sent → Android SMS type
_SMS_TYPE_MAP: dict[bool, int] = {
    False: _SMS_TYPE_INBOX,
    True:  _SMS_TYPE_SENT,
}

# Android MMS address type constants (X-Mms-Message-Type)
_MMS_ADDR_FROM = 137   # originator
_MMS_ADDR_TO   = 151   # recipient
_MMS_CHARSET   = 106   # UTF-8


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _esc_shell(value: str) -> str:
    """Escape single quotes for use inside a shell single-quoted string."""
    return value.replace("'", "\\'")


def _to_unix_ms(dt: datetime) -> int:
    """Convert a datetime to Unix milliseconds (Android SMS date column)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _to_unix_s(dt: datetime) -> int:
    """Convert a datetime to Unix seconds (Android MMS date column)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _parse_inserted_id(stdout: str, table: str) -> int | None:
    """
    Parse the row ID from an 'adb shell content insert' result line.
    Android prints:  Result: content://<table>/<id>
    Returns None when the output cannot be parsed.
    """
    m = re.search(rf"content://{re.escape(table)}/(\d+)", stdout)
    return int(m.group(1)) if m else None


def _is_injectable(msg: Message) -> bool:
    """Return True if this message can be injected into the Android SMS store."""
    if msg.service == "imessage":
        logger.debug(
            "inject_sms_android: skipping iMessage from %s (cannot recreate on Android)",
            msg.sender,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Content-provider path
# ---------------------------------------------------------------------------

def _insert_via_content_provider(
    adb: ADBManager,
    serial: str,
    msg: Message,
) -> bool:
    """
    Insert a single SMS into the system SMS content provider.

    Returns True on success (rc == 0), False otherwise.
    Android 10+ may return non-zero even if the insert superficially worked;
    callers should log a warning rather than treating this as fatal.
    """
    address = _esc_shell(msg.sender if not msg.is_sent else msg.recipient)
    body    = _esc_shell(msg.body)
    date_ms = _to_unix_ms(msg.timestamp)
    read_int  = 1 if msg.read else 0

    # Use the preserved sms_type if available, otherwise auto-detect
    sms_type = msg.sms_type if msg.sms_type > 0 else _SMS_TYPE_MAP[msg.is_sent]

    # Route to correct sub-URI: inbox, sent, draft, outbox, failed, queued
    _URI_MAP = {
        1: "content://sms/inbox",
        2: "content://sms/sent",
        3: "content://sms/draft",
        4: "content://sms/outbox",
        5: "content://sms/failed",
        6: "content://sms/queued",
    }
    uri = _URI_MAP.get(sms_type, "content://sms/sent" if msg.is_sent else "content://sms/inbox")

    # Build bind args — include thread_id and status if available
    bind_args = (
        f"--bind address:s:'{address}' "
        f"--bind body:s:'{body}' "
        f"--bind date:i:{date_ms} "
        f"--bind read:i:{read_int} "
        f"--bind type:i:{sms_type}"
    )
    if msg.thread_id > 0:
        bind_args += f" --bind thread_id:i:{msg.thread_id}"
    if msg.status >= 0:
        bind_args += f" --bind status:i:{msg.status}"

    _, stderr, rc = adb.shell(
        serial,
        f"content insert --uri {uri} {bind_args}",
        timeout=15,
    )

    if rc != 0:
        # Check whether it looks like a permissions error vs. a real failure
        if "SecurityException" in stderr or "permission" in stderr.lower():
            logger.warning(
                "inject_sms_android: SMS content provider rejected insert "
                "(Android 10+ permission restriction). rc=%d: %s",
                rc, stderr.strip(),
            )
        else:
            logger.debug(
                "inject_sms_android: content insert returned rc=%d: %s",
                rc, stderr.strip(),
            )
        return False

    return True


# ---------------------------------------------------------------------------
# Rooted path — direct sqlite3 insert
# ---------------------------------------------------------------------------

def _build_sql_inserts(messages: list[Message]) -> str:
    """
    Build a sequence of SQL INSERT statements for the ``sms`` table in
    mmssms.db.

    Only the essential columns are populated:
    address, body, date, date_sent, type, read, seen.
    thread_id is left as 0 (Android will recalculate threads on next sync).
    """
    lines: list[str] = []
    for msg in messages:
        if not _is_injectable(msg):
            continue
        address  = msg.sender if not msg.is_sent else msg.recipient
        body     = msg.body.replace("'", "''")   # SQL single-quote escape
        date_ms  = _to_unix_ms(msg.timestamp)
        sms_type = msg.sms_type if msg.sms_type > 0 else _SMS_TYPE_MAP[msg.is_sent]
        read_int = 1 if msg.read else 0
        thread_id = msg.thread_id
        status   = msg.status if msg.status >= 0 else -1

        lines.append(
            f"INSERT INTO sms (thread_id, address, body, date, date_sent, "
            f"type, read, seen, locked, error_code, status) VALUES "
            f"({thread_id}, '{address.replace(chr(39), chr(39)*2)}', '{body}', "
            f"{date_ms}, {date_ms}, {sms_type}, {read_int}, 1, 0, 0, {status});"
        )
    return "\n".join(lines)


def _insert_via_sqlite(
    adb: ADBManager,
    serial: str,
    messages: list[Message],
    staging_dir: Path,
) -> int:
    """
    Write a SQL script and execute it via ``su -c sqlite3`` on the device.

    Returns the number of messages written to the SQL script (best-effort;
    we cannot easily verify individual row success from the sqlite3 exit code).
    """
    injectable = [m for m in messages if _is_injectable(m)]
    if not injectable:
        return 0

    sql = _build_sql_inserts(injectable)
    if not sql.strip():
        return 0

    local_sql = staging_dir / "sms_insert.sql"
    try:
        local_sql.write_text(sql, encoding="utf-8")
    except Exception as exc:
        logger.error(
            "inject_sms_android: failed to write SQL script to staging: %s", exc
        )
        return 0

    remote_sql = f"{_DEVICE_DIR}/sms_insert.sql"
    if not adb.push(serial, local_sql, remote_sql):
        logger.error(
            "inject_sms_android: failed to push SQL script to device."
        )
        return 0

    _, stderr, rc = adb.shell_root(
        serial,
        f"sqlite3 {_MMSSMS_DB} < {remote_sql}",
        timeout=60,
    )
    if rc != 0:
        logger.error(
            "inject_sms_android: sqlite3 rooted insert failed rc=%d: %s",
            rc, stderr.strip(),
        )
        return 0

    logger.info(
        "inject_sms_android: rooted sqlite3 insert completed for %d message(s).",
        len(injectable),
    )
    return len(injectable)


# ---------------------------------------------------------------------------
# MMS — content-provider path
# ---------------------------------------------------------------------------

def _insert_mms_via_content_provider(
    adb: ADBManager,
    serial: str,
    msg: "Message",
    staging_dir: Path,
) -> bool:
    """
    Insert one MMS message into the Android MMS content provider.

    Steps
    -----
    1. Insert the PDU record into ``content://mms`` → parse its new ID.
    2. Insert FROM and TO address records into ``content://mms/{id}/addr``.
    3. Insert the text body as a ``text/plain`` part.
    4. For each attachment: push the file to device, insert a part record.

    Returns True if the PDU was inserted and assigned an ID (best-effort;
    part failures are logged but do not flip the return value).
    """
    date_s  = _to_unix_s(msg.timestamp)
    msg_box = _SMS_TYPE_SENT if msg.is_sent else _SMS_TYPE_INBOX
    has_att = bool(msg.attachments)
    ct      = "application/vnd.wap.multipart.related" if has_att else "text/plain"
    address = _esc_shell(msg.recipient if msg.is_sent else msg.sender)

    stdout, stderr, rc = adb.shell(
        serial,
        f"content insert --uri content://mms "
        f"--bind content_type:s:{ct} "
        f"--bind msg_box:i:{msg_box} "
        f"--bind date:i:{date_s} "
        f"--bind read:i:1 "
        f"--bind seen:i:1",
        timeout=15,
    )

    mms_id = _parse_inserted_id(stdout, "mms")
    if mms_id is None:
        logger.debug(
            "inject_sms_android MMS: content insert returned no ID (rc=%d): %s",
            rc, stderr.strip(),
        )
        return False

    # ── Address records ──────────────────────────────────────────────────────
    addr_from_type = _MMS_ADDR_FROM
    addr_to_type   = _MMS_ADDR_TO

    if msg.is_sent:
        # TO = actual recipient; FROM = "insert-address" (self)
        _, _, rc = adb.shell(
            serial,
            f"content insert --uri content://mms/{mms_id}/addr "
            f"--bind address:s:'{address}' "
            f"--bind type:i:{addr_to_type} --bind charset:i:{_MMS_CHARSET}",
            timeout=10,
        )
        if rc != 0:
            logger.warning("inject_sms_android: MMS TO addr insert failed (rc=%d) for mms_id=%s", rc, mms_id)
        _, _, rc = adb.shell(
            serial,
            f"content insert --uri content://mms/{mms_id}/addr "
            f"--bind address:s:'insert-address' "
            f"--bind type:i:{addr_from_type} --bind charset:i:{_MMS_CHARSET}",
            timeout=10,
        )
        if rc != 0:
            logger.warning("inject_sms_android: MMS FROM addr insert failed (rc=%d) for mms_id=%s", rc, mms_id)
    else:
        # FROM = sender; TO omitted (we are the recipient)
        _, _, rc = adb.shell(
            serial,
            f"content insert --uri content://mms/{mms_id}/addr "
            f"--bind address:s:'{address}' "
            f"--bind type:i:{addr_from_type} --bind charset:i:{_MMS_CHARSET}",
            timeout=10,
        )
        if rc != 0:
            logger.warning("inject_sms_android: MMS FROM addr insert failed (rc=%d) for mms_id=%s", rc, mms_id)

    # ── Text body part ───────────────────────────────────────────────────────
    if msg.body:
        adb.shell(
            serial,
            f"content insert --uri content://mms/{mms_id}/part "
            f"--bind seq:i:0 --bind ct:s:text/plain "
            f"--bind chset:i:{_MMS_CHARSET} "
            f"--bind text:s:'{_esc_shell(msg.body)}'",
            timeout=15,
        )

    # ── Attachment parts ─────────────────────────────────────────────────────
    for seq, att in enumerate(msg.attachments, start=1):
        if att.local_path is None or not att.local_path.exists():
            logger.debug(
                "inject_sms_android MMS: attachment %s has no local file — skipping",
                att.filename,
            )
            continue
        safe_name = re.sub(r"[^\w.\-]", "_", att.filename)
        remote    = f"{_DEVICE_DIR}/mms_{mms_id}_{seq}_{safe_name}"
        if not adb.push(serial, att.local_path, remote, timeout=60):
            logger.warning(
                "inject_sms_android MMS: could not push attachment %s — skipping",
                att.filename,
            )
            continue
        adb.shell(
            serial,
            f"content insert --uri content://mms/{mms_id}/part "
            f"--bind seq:i:{seq} "
            f"--bind ct:s:{att.mime_type} "
            f"--bind name:s:'{_esc_shell(att.filename)}' "
            f"--bind _data:s:'{remote}'",
            timeout=15,
        )

    return True


# ---------------------------------------------------------------------------
# MMS — rooted SQLite path
# ---------------------------------------------------------------------------

def _build_mms_sql(messages: list) -> str:
    """
    Build SQL INSERT statements for the MMS PDU, addr, and part tables.

    Uses ``(SELECT max(_id) FROM pdu)`` within the same transaction to
    reference the just-inserted PDU row without needing last_insert_rowid()
    in a multi-statement script.
    """
    lines: list[str] = ["BEGIN;"]

    for msg in messages:
        if not _is_injectable(msg):
            continue
        date_s  = _to_unix_s(msg.timestamp)
        msg_box = _SMS_TYPE_SENT if msg.is_sent else _SMS_TYPE_INBOX
        has_att = bool(msg.attachments)
        ct      = "application/vnd.wap.multipart.related" if has_att else "text/plain"
        address = (msg.recipient if msg.is_sent else msg.sender).replace("'", "''")

        # PDU
        lines.append(
            f"INSERT INTO pdu (thread_id, date, date_sent, msg_box, read, seen, "
            f"m_type, ct, retr_st, d_tm, read_status, status, m_cls, d_rpt) "
            f"VALUES (0, {date_s}, {date_s}, {msg_box}, 1, 1, "
            f"128, '{ct}', 0, 0, NULL, -1, 'personal', 0);"
        )

        pdu_id_expr = "(SELECT max(_id) FROM pdu)"

        # FROM address
        from_addr = "insert-address" if msg.is_sent else address
        lines.append(
            f"INSERT INTO addr (msg_id, contact_id, address, type, charset) "
            f"VALUES ({pdu_id_expr}, 0, '{from_addr}', {_MMS_ADDR_FROM}, {_MMS_CHARSET});"
        )
        # TO address (only for outgoing)
        if msg.is_sent:
            lines.append(
                f"INSERT INTO addr (msg_id, contact_id, address, type, charset) "
                f"VALUES ({pdu_id_expr}, 0, '{address}', {_MMS_ADDR_TO}, {_MMS_CHARSET});"
            )

        # Text body part
        if msg.body:
            body_sql = msg.body.replace("'", "''")
            lines.append(
                f"INSERT INTO part (mid, seq, ct, name, chset, cl, data, text) "
                f"VALUES ({pdu_id_expr}, 0, 'text/plain', NULL, {_MMS_CHARSET}, NULL, NULL, '{body_sql}');"
            )

        # Attachment parts (binary data path — files must already be on device)
        for seq, att in enumerate(msg.attachments, start=1):
            fname = att.filename.replace("'", "''")
            mime  = att.mime_type.replace("'", "''")
            safe  = re.sub(r"[^\w.\-]", "_", att.filename)
            remote = f"{_DEVICE_DIR}/mms_att_{safe}"
            lines.append(
                f"INSERT INTO part (mid, seq, ct, name, chset, cl, data, text) "
                f"VALUES ({pdu_id_expr}, {seq}, '{mime}', '{fname}', "
                f"{_MMS_CHARSET}, NULL, '{remote}', NULL);"
            )

    lines.append("COMMIT;")
    return "\n".join(lines)


def _insert_mms_via_sqlite(
    adb: ADBManager,
    serial: str,
    messages: list,
    staging_dir: Path,
) -> int:
    """Root-only: inject MMS records directly into mmssms.db."""
    injectable = [m for m in messages if _is_injectable(m)]
    if not injectable:
        return 0

    # Push any attachment files to device first
    for msg in injectable:
        for att in msg.attachments:
            if att.local_path and att.local_path.exists():
                safe   = re.sub(r"[^\w.\-]", "_", att.filename)
                remote = f"{_DEVICE_DIR}/mms_att_{safe}"
                adb.push(serial, att.local_path, remote, timeout=60)

    sql = _build_mms_sql(injectable)
    local_sql = staging_dir / "mms_insert.sql"
    try:
        local_sql.write_text(sql, encoding="utf-8")
    except Exception as exc:
        logger.error("inject_sms_android MMS: failed to write SQL: %s", exc)
        return 0

    remote_sql = f"{_DEVICE_DIR}/mms_insert.sql"
    if not adb.push(serial, local_sql, remote_sql):
        logger.error("inject_sms_android MMS: failed to push SQL to device")
        return 0

    _, stderr, rc = adb.shell_root(serial, f"sqlite3 {_MMSSMS_DB} < {remote_sql}", timeout=60)
    if rc != 0:
        logger.error("inject_sms_android MMS: sqlite3 insert failed rc=%d: %s", rc, stderr.strip())
        return 0

    return len(injectable)


# ---------------------------------------------------------------------------
# Public inject function
# ---------------------------------------------------------------------------

def _elevate_sms_permissions(adb: ADBManager, serial: str) -> str | None:
    """
    Temporarily elevate ADB shell permissions so ``content insert`` into the
    SMS content provider works on Android 10+.

    Strategy (tried in order):
    1. ``appops set com.android.shell WRITE_SMS allow`` — grants the shell
       uid the WRITE_SMS app-op.  The SMS content provider on AOSP checks
       ``OP_WRITE_SMS`` before rejecting non-default-SMS-app callers.
    2. Temporarily swap the default SMS application to our companion package
       via ``settings put``.  The previous default is returned so the caller
       can restore it.

    Returns
    -------
    The original default SMS package name (to pass to :func:`_restore_sms_permissions`),
    or ``None`` if no changes were made.
    """
    original_sms_app: str | None = None

    # 1. Grant WRITE_SMS appop to ADB shell
    _, _, rc = adb.shell(serial, "appops set com.android.shell WRITE_SMS allow", timeout=10)
    if rc == 0:
        logger.info(
            "inject_sms_android: granted WRITE_SMS appop to com.android.shell"
        )
    else:
        logger.debug("inject_sms_android: appops WRITE_SMS grant returned rc=%d", rc)

    # 2. Save + swap default SMS app so content provider accepts inserts
    #    from the shell user (some OEMs check the default SMS package rather
    #    than the appop alone).
    stdout, _, rc = adb.shell(
        serial,
        "settings get secure sms_default_application",
        timeout=10,
    )
    if rc == 0 and stdout.strip() and stdout.strip() != "null":
        original_sms_app = stdout.strip()
        logger.info(
            "inject_sms_android: current default SMS app = %s", original_sms_app
        )

    return original_sms_app


def _restore_sms_permissions(
    adb: ADBManager,
    serial: str,
    original_sms_app: str | None,
) -> None:
    """
    Undo the changes made by :func:`_elevate_sms_permissions`.

    - Revokes the WRITE_SMS appop from the shell.
    - Restores the original default SMS application.
    """
    # Revoke WRITE_SMS appop
    adb.shell(serial, "appops set com.android.shell WRITE_SMS default", timeout=10)
    logger.debug("inject_sms_android: revoked WRITE_SMS appop from com.android.shell")

    # Restore original default SMS app
    if original_sms_app:
        _, _, rc = adb.shell(
            serial,
            f"settings put secure sms_default_application {original_sms_app}",
            timeout=10,
        )
        if rc == 0:
            logger.info(
                "inject_sms_android: restored default SMS app → %s",
                original_sms_app,
            )
        else:
            logger.warning(
                "inject_sms_android: failed to restore default SMS app %s (rc=%d)",
                original_sms_app, rc,
            )


def _get_api_level(adb: ADBManager, serial: str) -> int:
    """Return the device's Android API level (e.g. 29 for Android 10)."""
    stdout, _, rc = adb.shell(serial, "getprop ro.build.version.sdk", timeout=5)
    if rc == 0 and stdout.strip().isdigit():
        return int(stdout.strip())
    return 0


def _force_sms_role_via_adb(
    adb: ADBManager,
    serial: str,
    package: str,
    api_level: int,
) -> bool:
    """
    Force-assign the default SMS app role to *package* via ADB shell commands,
    bypassing the on-device RoleManager popup entirely.

    Strategy by API level:
    - API 29+ (Android 10+): ``cmd role add-role-holder android.app.role.SMS``
      This is the authoritative RoleManager CLI and works on all AOSP-based
      ROMs.  It does NOT require root — the ADB shell identity has the
      ``MANAGE_ROLE_HOLDERS`` permission implicitly.
    - API 26-28 (Android 8-9): ``settings put secure sms_default_application``
      Pre-RoleManager, the default SMS app is just a Settings value.

    Returns True if the command succeeded (rc == 0).
    """
    if api_level >= 29:
        # Android 10+ — use the role service directly
        _, stderr, rc = adb.shell(
            serial,
            f"cmd role add-role-holder android.app.role.SMS {package}",
            timeout=15,
        )
        if rc == 0:
            logger.info(
                "inject_sms_android: forced SMS role → %s via 'cmd role add-role-holder'",
                package,
            )
            return True
        else:
            logger.warning(
                "inject_sms_android: 'cmd role add-role-holder' failed (rc=%d): %s",
                rc, stderr.strip(),
            )
            # Fall through to settings put as backup

    # Android 8-9, or fallback if cmd role failed
    _, _, rc = adb.shell(
        serial,
        f"settings put secure sms_default_application {package}",
        timeout=10,
    )
    if rc == 0:
        logger.info(
            "inject_sms_android: set default SMS app → %s via 'settings put'",
            package,
        )
        return True
    else:
        logger.warning(
            "inject_sms_android: 'settings put sms_default_application' failed (rc=%d)",
            rc,
        )
        return False


def _restore_sms_role_via_adb(
    adb: ADBManager,
    serial: str,
    original_package: str | None,
    api_level: int,
) -> None:
    """
    Restore the original default SMS app after injection is complete.

    Uses ``cmd role add-role-holder`` on API 29+ (mirrors the force path)
    and falls back to ``settings put`` on older versions.
    """
    if not original_package:
        return

    if api_level >= 29:
        _, _, rc = adb.shell(
            serial,
            f"cmd role add-role-holder android.app.role.SMS {original_package}",
            timeout=15,
        )
        if rc == 0:
            logger.info(
                "inject_sms_android: restored SMS role → %s via 'cmd role'",
                original_package,
            )
            return
        # Fall through to settings put

    _, _, rc = adb.shell(
        serial,
        f"settings put secure sms_default_application {original_package}",
        timeout=10,
    )
    if rc == 0:
        logger.info(
            "inject_sms_android: restored default SMS app → %s via 'settings put'",
            original_package,
        )
    else:
        logger.warning(
            "inject_sms_android: failed to restore default SMS app %s (rc=%d)",
            original_package, rc,
        )


def _try_companion_sms_inject(
    adb: ADBManager,
    serial: str,
    messages: list[Message],
) -> int:
    """
    Attempt to inject SMS messages via the companion APK's TCP socket.

    The companion must be the default SMS application for ContentResolver
    writes to ``content://sms`` to be accepted on Android 10+.

    Role-acquisition strategy (tried in order, each step skipped once granted):

    1. **Check** — companion may already hold the role from a prior run.
    2. **ADB force** — ``cmd role add-role-holder`` (API 29+) or
       ``settings put`` (API 26-28).  No user interaction required.
       This is the primary path and handles the case where the on-device
       popup never shows.
    3. **On-device popup** — ``request_sms_role`` launches
       ``ChangeDefaultSmsActivity`` (RoleManager intent).  Poll for up
       to 30 s in case the user is present.
    4. **Proceed anyway** — if none of the above worked, the inject is
       attempted regardless; the companion will report PERMISSION_DENIED
       per-message and the caller can fall back to rooted sqlite.

    After injection the original default SMS app is always restored.

    Returns the number of messages injected, or 0 on failure.
    """
    import time
    from core.companion_app_protocol import CompanionClient, setup_adb_forward

    COMPANION_PKG = "com.phonetransfer.companion.debug"

    api_level = _get_api_level(adb, serial)
    logger.info(
        "inject_sms_android: device API level = %d", api_level
    )

    # Save original default SMS app
    stdout, _, rc = adb.shell(
        serial, "settings get secure sms_default_application", timeout=10,
    )
    original = stdout.strip() if rc == 0 and stdout.strip() != "null" else None
    logger.info(
        "inject_sms_android: original default SMS app = %s", original
    )

    injected = 0
    try:
        setup_adb_forward(adb, serial)
        with CompanionClient(timeout=60.0) as client:
            if not client.ping():
                logger.warning(
                    "inject_sms_android: companion not responding — skipping socket path"
                )
                return 0

            # ── Step 1: Check if companion already has SMS role ──────────
            role_status = client.send_recv({"cmd": "check_sms_role"})
            is_default = role_status.get("is_default_sms", False)

            # ── Step 2: Force via ADB (no popup) ────────────────────────
            if not is_default:
                logger.info(
                    "inject_sms_android: forcing SMS role to companion via ADB…"
                )
                forced = _force_sms_role_via_adb(
                    adb, serial, COMPANION_PKG, api_level
                )
                if forced:
                    # Give Android a moment to propagate the role change
                    time.sleep(1.0)
                    role_status = client.send_recv({"cmd": "check_sms_role"})
                    is_default = role_status.get("is_default_sms", False)
                    if is_default:
                        logger.info(
                            "inject_sms_android: SMS role confirmed after ADB force"
                        )
                    else:
                        logger.warning(
                            "inject_sms_android: ADB force cmd succeeded but "
                            "companion still doesn't see itself as default — "
                            "may be a RoleManager vs settings mismatch"
                        )

            # ── Step 3: On-device popup fallback ────────────────────────
            # Detect Xiaomi/MIUI for the workaround (Phase 5, Item #14).
            # MIUI intercepts the standard RoleManager intent; the companion
            # APK's ChangeDefaultSmsActivity handles this by trying MIUI's
            # SmsDefaultDialog first.
            if not is_default:
                _is_xiaomi = False
                try:
                    from core.device_quirks import DeviceQuirks
                    _dinfo = client.device_info()
                    _quirks = DeviceQuirks.from_device_info(_dinfo)
                    _is_xiaomi = _quirks.needs_sms_workaround
                except Exception:
                    pass

                if _is_xiaomi:
                    logger.info(
                        "inject_sms_android: Xiaomi/MIUI detected — "
                        "using MIUI-aware SMS role flow (extended timeout)"
                    )
                    # acquire_sms_role triggers ChangeDefaultSmsActivity
                    # which tries MIUI's SmsDefaultDialog first
                    try:
                        client.send_recv({"cmd": "acquire_sms_role"})
                    except Exception:
                        pass
                    # MIUI dialogs take longer — poll for 45s with slower interval
                    deadline = time.monotonic() + 45
                    while time.monotonic() < deadline:
                        time.sleep(2.5)
                        try:
                            role_status = client.send_recv({"cmd": "check_sms_role"})
                            if role_status.get("is_default_sms", False):
                                is_default = True
                                logger.info(
                                    "inject_sms_android: SMS role granted via MIUI flow"
                                )
                                break
                        except Exception:
                            pass
                    # MIUI retry: first dialog may have handed off to RoleManager
                    if not is_default:
                        logger.info(
                            "inject_sms_android: MIUI first attempt failed — retrying"
                        )
                        try:
                            client.send_recv({"cmd": "acquire_sms_role"})
                        except Exception:
                            pass
                        retry_deadline = time.monotonic() + 15
                        while time.monotonic() < retry_deadline:
                            time.sleep(1.5)
                            try:
                                role_status = client.send_recv({"cmd": "check_sms_role"})
                                if role_status.get("is_default_sms", False):
                                    is_default = True
                                    logger.info(
                                        "inject_sms_android: SMS role granted on MIUI retry"
                                    )
                                    break
                            except Exception:
                                pass
                else:
                    # Standard AOSP flow
                    logger.info(
                        "inject_sms_android: ADB force didn't work — "
                        "trying on-device RoleManager popup…"
                    )
                    try:
                        client.send_recv({"cmd": "request_sms_role"})
                    except Exception:
                        pass
                    # Poll for up to 30s (user needs to tap "Yes")
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        time.sleep(2.0)
                        try:
                            role_status = client.send_recv({"cmd": "check_sms_role"})
                            if role_status.get("is_default_sms", False):
                                is_default = True
                                logger.info(
                                    "inject_sms_android: SMS role granted via popup"
                                )
                                break
                        except Exception:
                            pass

            if not is_default:
                logger.warning(
                    "inject_sms_android: could not acquire SMS role — "
                    "attempting inject anyway (may fail with PERMISSION_DENIED)"
                )

            # ── Send SMS payload ────────────────────────────────────────
            items_payload = []
            for msg in messages:
                items_payload.append({
                    "sender":    msg.sender,
                    "recipient": msg.recipient,
                    "body":      msg.body,
                    "timestamp": _to_unix_ms(msg.timestamp) if msg.timestamp else None,
                    "is_sent":   msg.is_sent,
                    "read":      msg.read,
                    "service":   msg.service,
                    "sms_type":  msg.sms_type,
                    "thread_id": msg.thread_id,
                    "status":    msg.status,
                })

            response = client.inject("sms", items_payload)
            injected = int(response.get("injected", 0))

            security_blocked = response.get("security_blocked", False)
            if security_blocked and injected == 0:
                logger.warning(
                    "inject_sms_android: companion reports PERMISSION_DENIED — "
                    "SMS role was not acquired by any method"
                )
            else:
                logger.info(
                    "inject_sms_android: companion socket injected %d/%d SMS",
                    injected, len(messages),
                )
    except Exception as exc:
        logger.warning(
            "inject_sms_android: companion SMS inject failed: %s", exc
        )
    finally:
        # Always restore original default SMS app
        _restore_sms_role_via_adb(adb, serial, original, api_level)

    return injected


def inject(
    serial: str,
    items: list[Message],
    staging_dir: Path,
    is_rooted: bool = False,
) -> int:
    """
    Inject SMS/MMS messages into the Android device identified by *serial*.

    Injection paths (tried in order for SMS):
    1. Content-provider insert with elevated appops (WRITE_SMS granted to
       ADB shell).  Works on most Android 10–14 AOSP-based devices.
    2. Companion socket route — sends SMS data to the companion APK which
       writes via its own ContentResolver as the temporary default SMS app.
    3. Rooted SQLite fallback — direct insert into mmssms.db.

    Parameters
    ----------
    serial:      ADB device serial.
    items:       Messages to inject.  iMessage entries are skipped.
    staging_dir: Local directory for temporary files (used by rooted path).
    is_rooted:   When True, direct sqlite3 insert is attempted as a fallback
                 for messages that the content provider rejects.

    Returns
    -------
    int: Number of messages successfully injected.
    """
    if not items:
        logger.info("inject_sms_android: no messages to inject — done.")
        return 0

    injectable = [m for m in items if _is_injectable(m)]
    skipped    = len(items) - len(injectable)
    if skipped:
        logger.info("inject_sms_android: skipping %d iMessage-only message(s).", skipped)
    if not injectable:
        logger.info("inject_sms_android: nothing injectable after filtering.")
        return 0

    # Split by service type
    sms_msgs = [m for m in injectable if m.service != "mms"]
    mms_msgs = [m for m in injectable if m.service == "mms"]

    logger.info(
        "inject_sms_android: %d SMS + %d MMS into device %s (rooted=%s)",
        len(sms_msgs), len(mms_msgs), serial, is_rooted,
    )

    try:
        cfg = get_config()
        adb = ADBManager(cfg)
    except Exception as exc:
        logger.error("inject_sms_android: failed to initialise ADB: %s", exc)
        return 0

    staging_dir.mkdir(parents=True, exist_ok=True)
    try:
        adb.shell(serial, f"mkdir -p {_DEVICE_DIR}")
    except Exception as exc:
        logger.warning("inject_sms_android: mkdir -p %s error: %s", _DEVICE_DIR, exc)

    # ── Elevate SMS permissions for Android 10+ ──────────────────────────────
    # Grant WRITE_SMS appop to the ADB shell so content-provider inserts
    # are accepted by the SMS provider.  Revoked after all inserts.
    original_sms_app = _elevate_sms_permissions(adb, serial)

    success_count = 0

    # ── 1. SMS — content-provider insert ─────────────────────────────────────
    sms_cp_failed: list[Message] = []
    for i, msg in enumerate(sms_msgs):
        try:
            ok = _insert_via_content_provider(adb, serial, msg)
            if ok:
                success_count += 1
            else:
                sms_cp_failed.append(msg)
        except Exception as exc:
            logger.warning("inject_sms_android: SMS error on message %d: %s", i, exc)
            sms_cp_failed.append(msg)

    logger.info(
        "inject_sms_android: SMS content provider succeeded %d/%d.",
        success_count, len(sms_msgs),
    )

    # ── 2. Companion socket fallback for failed SMS ──────────────────────────
    if sms_cp_failed:
        logger.info(
            "inject_sms_android: trying companion socket for %d failed SMS…",
            len(sms_cp_failed),
        )
        try:
            companion_count = _try_companion_sms_inject(adb, serial, sms_cp_failed)
            if companion_count > 0:
                success_count += companion_count
                # Remove successfully injected messages from the failed list
                sms_cp_failed = sms_cp_failed[companion_count:]
                logger.info(
                    "inject_sms_android: companion injected %d SMS.", companion_count
                )
        except Exception as exc:
            logger.debug("inject_sms_android: companion SMS path error: %s", exc)

    # ── 3. Rooted SQLite fallback ────────────────────────────────────────────
    if sms_cp_failed and is_rooted:
        try:
            rooted_count = _insert_via_sqlite(adb, serial, sms_cp_failed, staging_dir)
            success_count += rooted_count
        except Exception as exc:
            logger.error("inject_sms_android: SMS rooted fallback error: %s", exc)
    elif sms_cp_failed:
        logger.warning(
            "inject_sms_android: %d SMS could not be injected "
            "(content provider + companion failed, rooted mode disabled).",
            len(sms_cp_failed),
        )

    # ── 4. MMS — content-provider insert + rooted fallback ───────────────────
    mms_cp_failed: list[Message] = []
    for i, msg in enumerate(mms_msgs):
        try:
            ok = _insert_mms_via_content_provider(adb, serial, msg, staging_dir)
            if ok:
                success_count += 1
            else:
                mms_cp_failed.append(msg)
        except Exception as exc:
            logger.warning("inject_sms_android: MMS error on message %d: %s", i, exc)
            mms_cp_failed.append(msg)

    logger.info(
        "inject_sms_android: MMS content provider succeeded %d/%d.",
        len(mms_msgs) - len(mms_cp_failed), len(mms_msgs),
    )

    if mms_cp_failed and is_rooted:
        try:
            rooted_count = _insert_mms_via_sqlite(adb, serial, mms_cp_failed, staging_dir)
            success_count += rooted_count
        except Exception as exc:
            logger.error("inject_sms_android: MMS rooted fallback error: %s", exc)
    elif mms_cp_failed:
        logger.warning(
            "inject_sms_android: %d MMS could not be injected "
            "(content provider failed, rooted mode disabled).",
            len(mms_cp_failed),
        )

    # ── Restore SMS permissions ──────────────────────────────────────────────
    _restore_sms_permissions(adb, serial, original_sms_app)

    logger.info(
        "inject_sms_android: total injected = %d/%d.",
        success_count, len(injectable),
    )
    return success_count
