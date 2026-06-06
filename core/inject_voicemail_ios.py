"""
inject_voicemail_ios.py

Inject voicemail records into an iOS device via the active backup injector.

This combines two patterns in one module:
  * **Override**: INSERT rows into ``HomeDomain:Library/Voicemail/voicemail.db``.
  * **Addition**: stage a ``<ROWID>.amr`` audio blob alongside each row,
    encrypted at protection class 4 (``NSFileProtectionCompleteUntilFirstUserAuthentication``)
    — the class iOS uses natively for voicemail audio.

Voicemail is not (yet) one of ``session_manager.ALL_CATEGORIES``.  This
module is callable directly from higher-level code that has a list of
:class:`VoicemailRecord` items and an active backup injector; it mirrors
the ``inject(device_id, items, staging_dir, is_privileged)`` signature
used by the rest of the pipeline so it can be dropped in later if
voicemail becomes a tracked category.

Schema detail (from G:/test/modify_voicemail.py):
  - ``voicemail.date`` / ``voicemail.expiration`` are UNIX seconds,
    NOT Apple epoch.  (sms.db, CallHistory, Calendar, Notes all use
    Apple epoch; voicemail is the odd one out.)
  - ``flags`` = 4362 (0x110A) means heard+read; no-op for fresh rows.
  - ``label`` is the account GUID.  We use whatever the first real row
    in the DB had, so injected rows share an account with the existing
    device state.
  - ``token`` is the carrier-side identifier; free-form string, any
    stable value works.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.ios_backup_injector import IOSBackupInjector, get_current_injector
from core.normalization_schema import Voicemail

logger = logging.getLogger(__name__)


_VM_DB_DOMAIN = "HomeDomain"
_VM_DB_RELPATH = "Library/Voicemail/voicemail.db"
_VM_AMR_REL_DIR = "Library/Voicemail"

_FLAGS_HEARD_READ = 4362  # 0x110A
_AMR_PROTECTION_CLASS = 4  # NSFileProtectionCompleteUntilFirstUserAuthentication
_DEFAULT_RETENTION_DAYS = 60


def inject(
    device_id: str,
    items: list[Voicemail],
    staging_dir: Path,
    is_privileged: bool,
) -> int:
    if not items:
        logger.info("inject_voicemail_ios: no voicemails for %s", device_id)
        return 0

    injector = get_current_injector()
    if injector is None:
        logger.warning(
            "inject_voicemail_ios: no backup injector active — voicemail "
            "injection requires a modifiable backup.  Skipping."
        )
        return 0

    return _inject_via_backup(injector, items)


def _inject_via_backup(
    injector: IOSBackupInjector, items: list[Voicemail]
) -> int:
    db_path = injector.stage_db(_VM_DB_DOMAIN, _VM_DB_RELPATH)

    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=FULL")

        max_rowid = con.execute(
            "SELECT COALESCE(MAX(ROWID), 0) FROM voicemail"
        ).fetchone()[0]
        max_uid = con.execute(
            "SELECT COALESCE(MAX(remote_uid), 0) FROM voicemail"
        ).fetchone()[0]

        # Borrow the account label + receiver from a real row when one exists
        # — iOS refuses to play voicemails whose label doesn't match a
        # known mailbox.
        sample = con.execute(
            "SELECT label, receiver FROM voicemail "
            "WHERE label IS NOT NULL LIMIT 1"
        ).fetchone()
        account_label = sample[0] if sample else str(uuid.uuid4()).upper()
        receiver = sample[1] if sample else ""

        inserted = 0
        with con:
            for i, vm in enumerate(items, start=1):
                new_rowid = max_rowid + i
                new_uid = max_uid + i

                received = vm.received or datetime.now(timezone.utc)
                if received.tzinfo is None:
                    received = received.replace(tzinfo=timezone.utc)
                vm_date = int(received.timestamp())
                vm_expiration = vm_date + _DEFAULT_RETENTION_DAYS * 86400

                token = vm.token or (
                    f"<{received.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                    f"-phonetransfer-{uuid.uuid4().hex[:24]}>"
                )

                con.execute(
                    """
                    INSERT INTO voicemail (
                        ROWID, remote_uid, date, token,
                        sender, callback_num,
                        duration, expiration, trashed_date, flags,
                        receiver, label, uuid
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
                    """,
                    (
                        new_rowid, new_uid, vm_date, token,
                        vm.sender, vm.sender,
                        int(vm.duration_seconds or 0), vm_expiration,
                        _FLAGS_HEARD_READ,
                        receiver, account_label, str(uuid.uuid4()).upper(),
                    ),
                )

                injector.stage_addition(
                    _VM_DB_DOMAIN,
                    f"{_VM_AMR_REL_DIR}/{new_rowid}.amr",
                    vm.audio_bytes,
                    protection_class=_AMR_PROTECTION_CLASS,
                )

                inserted += 1

            seq_row = con.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='voicemail'"
            ).fetchone()
            new_max = max_rowid + inserted
            if seq_row:
                con.execute(
                    "UPDATE sqlite_sequence SET seq=? WHERE name='voicemail'",
                    (new_max,),
                )
            else:
                con.execute(
                    "INSERT INTO sqlite_sequence (name, seq) VALUES "
                    "('voicemail', ?)",
                    (new_max,),
                )
    finally:
        con.close()

    return inserted
