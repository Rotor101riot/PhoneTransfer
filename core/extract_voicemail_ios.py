"""
extract_voicemail_ios.py

Extract voicemails from an iOS device backup.

Source of truth:
  - ``HomeDomain/Library/Voicemail/voicemail.db`` — SQLite table ``voicemail``
    with ROWID, sender, date (UNIX seconds), duration, etc.
  - ``HomeDomain/Library/Voicemail/<ROWID>.amr`` — the audio payload.

Returns ``list[Voicemail]`` per the normalized schema.  Voicemails whose
.amr file cannot be located are still returned, but with
``audio_bytes = b""`` so a downstream injector can decide whether to skip
them or emit a placeholder.

Note: voicemail.date is UNIX epoch seconds, NOT Apple epoch — unlike every
other iOS database we touch in this pipeline.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from convert.convert_audio import normalize_voicemail_audio
from core.normalization_schema import Voicemail

logger = logging.getLogger(__name__)


_VM_DOMAIN = "HomeDomain"
_VM_DB_RELPATH = "Library/Voicemail/voicemail.db"
_VM_AMR_DIR = "Library/Voicemail"


def extract(
    udid: str, staging_dir: Path, is_jailbroken: bool = False
) -> list[Voicemail]:
    try:
        return _extract_impl(udid, staging_dir, is_jailbroken)
    except Exception as exc:
        logger.exception(
            "extract_voicemail_ios: top-level failure for %s: %s", udid, exc
        )
        return []


def _extract_impl(
    udid: str, staging_dir: Path, is_jailbroken: bool
) -> list[Voicemail]:
    work_dir = staging_dir / "voicemail_ios"
    work_dir.mkdir(parents=True, exist_ok=True)

    local_db = work_dir / "voicemail.db"
    if not _pull_file(udid, _VM_DB_RELPATH, local_db):
        logger.info(
            "extract_voicemail_ios: no voicemail.db for %s (likely empty mailbox)",
            udid,
        )
        return []

    rows = _read_rows(local_db)
    if not rows:
        return []

    voicemails: list[Voicemail] = []
    for rowid, sender, vm_date, duration in rows:
        amr_rel = f"{_VM_AMR_DIR}/{rowid}.amr"
        local_amr = work_dir / f"{rowid}.amr"
        audio = b""
        if _pull_file(udid, amr_rel, local_amr):
            try:
                raw = local_amr.read_bytes()
                audio = normalize_voicemail_audio(raw, work_dir)
            except Exception as exc:
                logger.debug(
                    "extract_voicemail_ios: could not read %s: %s",
                    local_amr, exc,
                )

        received = datetime.fromtimestamp(
            int(vm_date or 0), tz=timezone.utc
        )
        voicemails.append(
            Voicemail(
                sender=sender or "",
                received=received,
                duration_seconds=int(duration or 0),
                audio_bytes=audio,
                audio_mime="audio/amr",
            )
        )

    logger.info(
        "extract_voicemail_ios: extracted %d voicemail(s) for %s",
        len(voicemails), udid,
    )
    return voicemails


def _read_rows(db_path: Path) -> list[tuple]:
    try:
        con = sqlite3.connect(str(db_path))
    except Exception as exc:
        logger.warning(
            "extract_voicemail_ios: can't open %s: %s", db_path, exc
        )
        return []
    try:
        return con.execute(
            "SELECT ROWID, sender, date, duration FROM voicemail "
            "WHERE COALESCE(trashed_date, 0) = 0 ORDER BY date DESC"
        ).fetchall()
    except Exception as exc:
        logger.warning(
            "extract_voicemail_ios: query failed on %s: %s", db_path, exc
        )
        return []
    finally:
        con.close()


def _pull_file(udid: str, relative_path: str, dest: Path) -> bool:
    """Pull a HomeDomain file out of the backup to *dest*.  Returns success."""
    try:
        from core.device_connection_cache import get_iosbackup
        dest.parent.mkdir(parents=True, exist_ok=True)
        backup = get_iosbackup(udid)
        info = backup.getFileDecryptedCopy(
            relativePath=relative_path,
            targetName=dest.name,
            targetFolder=str(dest.parent),
        )
        if info and dest.exists() and dest.stat().st_size > 0:
            return True
    except Exception as exc:
        logger.debug(
            "extract_voicemail_ios: iOSbackup pull of %s failed: %s",
            relative_path, exc,
        )
    return False
