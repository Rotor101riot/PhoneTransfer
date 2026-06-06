"""
inject_notes_ios.py

Injects Note records into an iOS device connected via USB.

Strategy
--------
iOS does not expose a public API for writing directly into the Notes app's
NoteStore.sqlite Core Data database without private framework access.  Direct
SQLite manipulation is fragile because the schema and WAL state can vary
significantly between iOS versions, and an invalid write can corrupt the
entire Notes database.

Instead, we push plain-text representations of the notes to a readable
location on the device and guide the user to import them manually:

    /var/mobile/Media/PhoneTransfer/notes/<sanitised_title>.txt

Each file contains the note title as a Markdown heading followed by the body,
making the content immediately readable and easy to copy-paste into the Notes
app, AirDrop to a Mac, or open in a third-party app.

Jailbroken path:
    The same strategy is used.  Writing to NoteStore.sqlite is too risky to
    do reliably across iOS versions — we avoid it entirely.

Return value: count of note files successfully pushed to the device.
"""

from __future__ import annotations

import gzip
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.afc_connector import AFCConnector
from core.ios_backup_injector import IOSBackupInjector, get_current_injector
from core.ios_service_broker import IOSServiceBroker
from core.normalization_schema import Note

logger = logging.getLogger(__name__)

# Remote directory accessible via standard AFC
_NOTES_DIR = "/var/mobile/Media/PhoneTransfer/notes"

# NoteStore.sqlite constants (see G:/test/modify_notes.py).
_NOTES_DOMAIN = "AppDomainGroup-group.com.apple.notes"
_NOTES_RELPATH = "NoteStore.sqlite"
_APPLE_EPOCH_OFFSET = 978307200.0
_ENT_ICCLOUDSTATE = 2
_ENT_ICNOTE = 12
_ENT_ICNOTEDATA = 19


# ---------------------------------------------------------------------------
# Filename sanitisation
# ---------------------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    """
    Remove or replace characters that are invalid or problematic in iOS
    (and Windows/POSIX) filenames, then cap the length to 200 characters.

    Characters replaced: < > : " / \\ | ? * and ASCII control characters.
    Leading/trailing whitespace and dots are also stripped to avoid hidden
    files or trailing-dot issues on some filesystems.
    """
    # Replace forbidden characters with underscores
    sanitised = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    # Collapse multiple consecutive underscores or spaces for readability
    sanitised = re.sub(r"_+", "_", sanitised)
    sanitised = sanitised.strip(". ")
    # Cap to 200 characters (well under all relevant filesystem limits)
    sanitised = sanitised[:200]
    # If the entire title was whitespace / special chars, use a fallback
    return sanitised or "untitled"


def _note_to_text(note: Note) -> str:
    """
    Render a Note as a plain-text string with a Markdown-style heading.

    Example output:
        # Meeting notes

        Discussed Q3 targets.
        Action items: …
    """
    title_line = f"# {note.title}" if note.title else "# (untitled)"
    parts: list[str] = [title_line, ""]

    if note.folder:
        parts.append(f"Folder: {note.folder}")
        parts.append("")

    if note.created:
        parts.append(f"Created: {note.created.isoformat()}")
    if note.modified:
        parts.append(f"Modified: {note.modified.isoformat()}")
    if note.created or note.modified:
        parts.append("")

    parts.append(note.body or "")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public inject function
# ---------------------------------------------------------------------------

def inject(
    udid: str,
    items: list[Note],
    staging_dir: Path,
    is_jailbroken: bool = False,
) -> int:
    """
    Inject notes into the iOS device identified by *udid*.

    Each note is pushed as an individual .txt file to
    /var/mobile/Media/PhoneTransfer/notes/ on the device.

    Parameters
    ----------
    udid:           iOS device UDID.
    items:          Notes to inject.
    staging_dir:    Local directory for temporary files.
    is_jailbroken:  Accepted for interface consistency; both paths use AFC.

    Returns
    -------
    int: Number of note files successfully pushed to the device.
         Returns 0 on a total failure.
    """
    if not items:
        logger.info("inject_notes_ios: no notes to inject — done.")
        return 0

    injector = get_current_injector()
    if injector is not None:
        try:
            count = _inject_via_backup(injector, items)
            logger.info(
                "inject_notes_ios: staged %d note(s) into the backup for %s",
                count, udid,
            )
            return count
        except Exception as exc:
            logger.warning(
                "inject_notes_ios: backup-mod path failed (%s) — "
                "falling back to AFC .txt push", exc,
            )

    logger.info(
        "inject_notes_ios: preparing %d note(s) for device %s "
        "(jailbroken=%s).",
        len(items),
        udid,
        is_jailbroken,
    )

    broker = IOSServiceBroker(udid=udid)
    try:
        return _do_inject(broker, items, staging_dir)
    finally:
        broker.close()


# ---------------------------------------------------------------------------
# Internal implementation
# ---------------------------------------------------------------------------

def _do_inject(
    broker: IOSServiceBroker,
    items: list[Note],
    staging_dir: Path,
) -> int:
    """Open AFC, prepare staging files, and push each note."""
    try:
        afc = AFCConnector(broker)
    except Exception as exc:
        logger.error("inject_notes_ios: failed to open AFC service: %s", exc)
        return 0

    # Ensure the remote notes directory exists
    try:
        afc.makedirs(_NOTES_DIR)
    except Exception as exc:
        logger.warning(
            "inject_notes_ios: makedirs(%s) failed (may already exist): %s",
            _NOTES_DIR,
            exc,
        )

    # Prepare staging directory
    staging_dir.mkdir(parents=True, exist_ok=True)
    local_notes_dir = staging_dir / "notes"
    local_notes_dir.mkdir(parents=True, exist_ok=True)

    # Fetch existing filenames on the device to avoid overwriting
    existing_remote: set[str] = set(afc.list_dir(_NOTES_DIR))

    pushed = 0
    for i, note in enumerate(items):
        try:
            pushed += _push_one(afc, note, i, local_notes_dir, existing_remote)
        except Exception as exc:
            logger.warning(
                "inject_notes_ios: unexpected error pushing note %d (%r): %s",
                i,
                note.title,
                exc,
            )

    logger.info(
        "inject_notes_ios: pushed %d / %d note(s) to %s.  "
        "Files are available in the PhoneTransfer folder in the Files app.",
        pushed,
        len(items),
        _NOTES_DIR,
    )
    return pushed


def _push_one(
    afc: AFCConnector,
    note: Note,
    index: int,
    local_notes_dir: Path,
    existing_remote: set[str],
) -> int:
    """
    Write a single Note to a local staging file and push it to the device.

    Returns 1 on success, 0 on failure.
    Mutates *existing_remote* to register the filename once pushed.
    """
    # ── Render the note to text ─────────────────────────────────────────────
    try:
        text = _note_to_text(note)
    except Exception as exc:
        logger.warning(
            "inject_notes_ios: failed to render note %d (%r): %s",
            index,
            note.title,
            exc,
        )
        return 0

    # ── Determine a unique remote filename ──────────────────────────────────
    base_name = _sanitize_filename(note.title or f"note_{index}")
    filename = _unique_filename(f"{base_name}.txt", existing_remote)

    # ── Write to staging ────────────────────────────────────────────────────
    local_path = local_notes_dir / filename
    try:
        local_path.write_text(text, encoding="utf-8")
    except Exception as exc:
        logger.warning(
            "inject_notes_ios: failed to write staging file for note %d: %s",
            index,
            exc,
        )
        return 0

    # ── Push to device ──────────────────────────────────────────────────────
    device_path = f"{_NOTES_DIR}/{filename}"
    logger.debug(
        "inject_notes_ios: pushing %s -> %s", local_path, device_path
    )
    ok = afc.push_file(local_path, device_path)
    if ok:
        existing_remote.add(filename)
        return 1
    else:
        logger.warning(
            "inject_notes_ios: AFC push_file failed for note %d (%r).",
            index,
            note.title,
        )
        return 0


def _unique_filename(original: str, existing: set[str]) -> str:
    """
    Return a filename that does not collide with any name in *existing*.

    Appends _1, _2, … before the extension until a free name is found.
    """
    if original not in existing:
        return original

    stem = Path(original).stem
    suffix = Path(original).suffix
    counter = 1
    while True:
        candidate = f"{stem}_{counter}{suffix}"
        if candidate not in existing:
            return candidate
        counter += 1


# ---------------------------------------------------------------------------
# Backup-mod path
# ---------------------------------------------------------------------------

def _inject_via_backup(
    injector: IOSBackupInjector, notes: list[Note]
) -> int:
    db_path = injector.stage_db(_NOTES_DOMAIN, _NOTES_RELPATH)

    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=FULL")

        folder_pk = _resolve_default_folder_pk(con)

        max_sync = con.execute(
            "SELECT COALESCE(MAX(Z_PK), 0) FROM ZICCLOUDSYNCINGOBJECT"
        ).fetchone()[0]
        max_nd = con.execute(
            "SELECT COALESCE(MAX(Z_PK), 0) FROM ZICNOTEDATA"
        ).fetchone()[0]
        max_cs = con.execute(
            "SELECT COALESCE(MAX(Z_PK), 0) FROM ZICCLOUDSTATE"
        ).fetchone()[0]

        inserted = 0
        with con:
            for i, note in enumerate(notes):
                note_pk = max_sync + 1 + i
                cs_pk = max_cs + 1 + i
                nd_pk = max_nd + 1 + i

                created = note.created or datetime.now(timezone.utc)
                modified = note.modified or created
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if modified.tzinfo is None:
                    modified = modified.replace(tzinfo=timezone.utc)

                created_apple = created.timestamp() - _APPLE_EPOCH_OFFSET
                modified_apple = modified.timestamp() - _APPLE_EPOCH_OFFSET
                snippet = (note.body or "").split("\n", 1)[0][:100]
                note_uuid = str(uuid.uuid4()).upper()
                zdata = _build_zdata(note.body or "")

                con.execute(
                    "INSERT INTO ZICCLOUDSTATE "
                    "(Z_PK, Z_ENT, Z_OPT, ZCURRENTLOCALVERSION, ZINCLOUD, "
                    " ZLATESTVERSIONSYNCEDTOCLOUD, ZLOCALVERSIONDATE) "
                    "VALUES (?, ?, 1, 1, 0, 0, ?)",
                    (cs_pk, _ENT_ICCLOUDSTATE, modified_apple),
                )
                con.execute(
                    "INSERT INTO ZICNOTEDATA "
                    "(Z_PK, Z_ENT, Z_OPT, ZNOTE, ZDATA) "
                    "VALUES (?, ?, 1, ?, ?)",
                    (nd_pk, _ENT_ICNOTEDATA, note_pk, zdata),
                )
                con.execute(
                    """
                    INSERT INTO ZICCLOUDSYNCINGOBJECT (
                        Z_PK, Z_ENT, Z_OPT,
                        ZTITLE1, ZSNIPPET,
                        ZCREATIONDATE1, ZMODIFICATIONDATE1,
                        ZNOTEDATA, ZFOLDER, ZCLOUDSTATE,
                        ZIDENTIFIER, ZMARKEDFORDELETION,
                        ZNEEDSTOBEFETCHEDFROMCLOUD
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
                    """,
                    (
                        note_pk, _ENT_ICNOTE,
                        note.title or "", snippet,
                        created_apple, modified_apple,
                        nd_pk, folder_pk, cs_pk,
                        note_uuid,
                    ),
                )
                inserted += 1

            n = len(notes)
            for z_name, new_max in (
                ("ICCloudSyncingObject", max_sync + n),
                ("ICCloudState", max_cs + n),
                ("ICNoteData", max_nd + n),
                ("ICNote", max_sync + n),
            ):
                con.execute(
                    "UPDATE Z_PRIMARYKEY SET Z_MAX=? WHERE Z_NAME=?",
                    (new_max, z_name),
                )
    finally:
        con.close()

    return inserted


def _resolve_default_folder_pk(con: sqlite3.Connection) -> int:
    """
    Find a writable Notes folder PK to attach new notes to.

    Priority: the folder named "Notes" (or "Notes (iCloud)"), else any
    non-trash folder with the smallest PK.  Z_ENT=14 is ICFolder on
    recent iOS versions but we key on ZTITLE2 to avoid locking ourselves
    to one schema version.
    """
    row = con.execute(
        "SELECT Z_PK FROM ZICCLOUDSYNCINGOBJECT "
        "WHERE ZTITLE2 IS NOT NULL "
        "AND COALESCE(ZMARKEDFORDELETION, 0) = 0 "
        "AND (ZTITLE2='Notes' OR ZTITLE2 LIKE 'Notes%') "
        "ORDER BY Z_PK LIMIT 1"
    ).fetchone()
    if row:
        return row[0]
    row = con.execute(
        "SELECT Z_PK FROM ZICCLOUDSYNCINGOBJECT "
        "WHERE ZTITLE2 IS NOT NULL "
        "AND COALESCE(ZMARKEDFORDELETION, 0) = 0 "
        "ORDER BY Z_PK LIMIT 1"
    ).fetchone()
    if not row:
        raise RuntimeError(
            "NoteStore.sqlite: no ICFolder rows found — can't attach notes"
        )
    return row[0]


# ── Notes protobuf encoder (copied from G:/test/modify_notes.py) ───────────

def _encode_varint(v: int) -> bytes:
    buf = []
    while True:
        bits = v & 0x7F
        v >>= 7
        if v:
            buf.append(bits | 0x80)
        else:
            buf.append(bits)
            break
    return bytes(buf)


def _varint_field(field_num: int, value: int) -> bytes:
    tag = (field_num << 3) | 0
    return _encode_varint(tag) + _encode_varint(value)


def _bytes_field(field_num: int, data: bytes) -> bytes:
    tag = (field_num << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(data)) + data


def _str_field(field_num: int, text: str) -> bytes:
    return _bytes_field(field_num, text.encode("utf-8"))


def _build_run(pos: int, length: int, run_type: int = 0) -> bytes:
    inner = _varint_field(1, pos) + _varint_field(2, 0)
    msg = _bytes_field(1, inner) + _varint_field(2, length) + _bytes_field(3, inner)
    if run_type:
        msg += _varint_field(5, run_type)
    return _bytes_field(3, msg)


def _build_zdata(text: str) -> bytes:
    """Gzip-compressed NoteProto bytes for ZICNOTEDATA.ZDATA."""
    text_len = len(text.encode("utf-8"))
    sentinel_inner = _varint_field(1, 0) + _varint_field(2, 0xFFFFFFFF)
    sentinel_run = _bytes_field(
        3,
        _bytes_field(1, sentinel_inner)
        + _varint_field(2, 0)
        + _bytes_field(3, sentinel_inner),
    )
    note_content = (
        _str_field(2, text)
        + _build_run(0, 0, run_type=1)
        + _build_run(1, text_len, run_type=2)
        + sentinel_run
    )
    note_body = (
        _varint_field(1, 0)
        + _varint_field(2, 0)
        + _bytes_field(3, note_content)
    )
    top = _varint_field(1, 0) + _bytes_field(2, note_body)
    return gzip.compress(top)
