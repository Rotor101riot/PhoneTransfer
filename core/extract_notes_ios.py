"""
extract_notes_ios.py

Extracts Notes from an iOS device.
NoteStore.sqlite via AFC2 (jailbroken) or iOSbackup (non-jailbroken).
Body blobs are zlib-compressed protobuf; text fragments are extracted via wire scanning.
Apple timestamp epoch: 2001-01-01 UTC.
"""

from __future__ import annotations

import logging
import sqlite3
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_STAGING_SUBDIR = "notes_ios"


def extract(udid: str, staging_dir: Path, is_jailbroken: bool = False) -> list:
    work_dir = staging_dir / _STAGING_SUBDIR
    work_dir.mkdir(parents=True, exist_ok=True)
    db_path = work_dir / "NoteStore.sqlite"
    try:
        _fetch_db(udid, db_path, is_jailbroken)
    except Exception as exc:
        logger.error("Notes: failed to fetch NoteStore.sqlite: %s", exc)
        return []
    if not db_path.exists():
        logger.warning("Notes: NoteStore.sqlite not available for %s", udid)
        return []
    try:
        return _parse_notes(db_path)
    except Exception as exc:
        logger.exception("Notes: unexpected error: %s", exc)
        return []


def _fetch_db(udid: str, dest: Path, is_jailbroken: bool) -> None:
    if is_jailbroken and _try_afc2(udid, dest):
        return
    _try_iOSbackup(udid, dest)


def _try_afc2(udid: str, dest: Path) -> bool:
    try:
        from core.afc2_connector import AFC2Connector
        with AFC2Connector(udid) as afc2:
            root = "/var/mobile/Containers/Shared/AppGroup"
            try:
                uuids = afc2.list_dir(root)
            except Exception:
                uuids = []
            for uuid_name in uuids:
                candidate = f"{root}/{uuid_name}/NoteStore.sqlite"
                data = afc2.read_file(candidate)
                if data and data[:6] == b"SQLite":
                    dest.write_bytes(data)
                    logger.info("Notes: AFC2 pull OK from %s", candidate)
                    return True
        return False
    except (PermissionError, ImportError):
        return False
    except Exception as exc:
        logger.debug("Notes: AFC2 failed: %s", exc)
        return False


def _try_iOSbackup(udid: str, dest: Path) -> None:
    # NoteStore.sqlite moved to AppDomainGroup-group.com.apple.notes in iOS 13+.
    # Try both domains; fall back to an unscoped search if both fail.
    _DOMAINS = [
        "AppDomainGroup-group.com.apple.notes",
        "HomeDomain",
    ]
    try:
        from core.device_connection_cache import get_iosbackup
        b = get_iosbackup(udid)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # iOSbackup searches all domains by relativePath automatically.
        try:
            if dest.exists():
                dest.unlink()
            info = b.getFileDecryptedCopy(
                relativePath="NoteStore.sqlite",
                targetName=dest.name,
                targetFolder=str(dest.parent),
            )
            if info and dest.exists():
                logger.info("Notes: iOSbackup pull OK")
                return
        except Exception as exc:
            logger.debug("Notes: iOSbackup pull failed: %s", exc)
    except ImportError:
        logger.debug("Notes: iOSbackup not installed")
    except Exception as exc:
        logger.debug("Notes: iOSbackup failed: %s", exc)


def _parse_notes(db_path: Path) -> list:
    notes: list = []
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        if _table_exists(con, "ZNOTE"):
            notes = _parse_notes_legacy(con)
        elif _table_exists(con, "ZICCLOUDSYNCINGOBJECT"):
            notes = _parse_notes_modern(con)
        else:
            logger.warning("Notes: unrecognised schema in %s — no ZNOTE or ZICCLOUDSYNCINGOBJECT table", db_path.name)
    except Exception as exc:
        logger.exception("Notes: DB parse error: %s", exc)
    finally:
        con.close()
    logger.info("Notes: extracted %d notes", len(notes))
    return notes


def _parse_notes_legacy(con: sqlite3.Connection) -> list:
    """Parse the pre-iOS-13 ZNOTE schema."""
    from core.normalization_schema import Note
    notes: list = []
    cols = _col_names(con, "ZNOTE")
    t_col = "ZTITLE1"            if "ZTITLE1"            in cols else "ZTITLE"
    c_col = "ZCREATIONDATE1"     if "ZCREATIONDATE1"     in cols else "ZCREATIONDATE"
    m_col = "ZMODIFICATIONDATE1" if "ZMODIFICATIONDATE1" in cols else "ZMODIFICATIONDATE"
    b_join = b_sel = f_join = f_sel = ""
    if _table_exists(con, "ZNOTEBODY") and "ZCONTENT" in _col_names(con, "ZNOTEBODY"):
        b_join = "LEFT JOIN ZNOTEBODY ON ZNOTEBODY.ZNOTE = ZNOTE.Z_PK"
        b_sel  = ", ZNOTEBODY.ZCONTENT AS ZBODYCONTENT"
    if "ZFOLDER" in cols and _table_exists(con, "ZFOLDER"):
        f_join = "LEFT JOIN ZFOLDER ON ZFOLDER.Z_PK = ZNOTE.ZFOLDER"
        f_sel  = ", ZFOLDER.ZTITLE2 AS ZFOLDERNAME"
    sql = (f"SELECT ZNOTE.{t_col} AS ZTITLE, ZNOTE.{c_col} AS ZCREATED,"
           f" ZNOTE.{m_col} AS ZMODIFIED {b_sel} {f_sel}"
           f" FROM ZNOTE {b_join} {f_join}"
           f" WHERE ZNOTE.ZISPASSWORDPROTECTED=0 OR ZNOTE.ZISPASSWORDPROTECTED IS NULL")
    for row in con.execute(sql):
        keys = row.keys()
        notes.append(Note(
            title    = row["ZTITLE"] or "Untitled",
            body     = _decode_body(row["ZBODYCONTENT"] if "ZBODYCONTENT" in keys else None),
            created  = _apple_ts(row["ZCREATED"]),
            modified = _apple_ts(row["ZMODIFIED"]),
            folder   = row["ZFOLDERNAME"] if "ZFOLDERNAME" in keys else None,
        ))
    return notes


def _parse_notes_modern(con: sqlite3.Connection) -> list:
    """
    Parse the iOS 13+ ZICCLOUDSYNCINGOBJECT schema.

    Notes, folders, and attachments all share ZICCLOUDSYNCINGOBJECT; notes
    are identified by having a non-NULL ZNOTEDATA FK into ZICNOTEDATA.
    """
    from core.normalization_schema import Note
    notes: list = []
    ico_cols = _col_names(con, "ZICCLOUDSYNCINGOBJECT")

    # Title column name varies slightly across iOS versions
    t_col = "ZTITLE1" if "ZTITLE1" in ico_cols else "ZTITLE"
    m_col = "ZMODIFICATIONDATE1" if "ZMODIFICATIONDATE1" in ico_cols else "ZMODIFICATIONDATE"

    # Body data lives in ZICNOTEDATA.ZDATA (protobuf blob)
    has_notedata = _table_exists(con, "ZICNOTEDATA")
    b_join = b_sel = ""
    if has_notedata:
        b_join = "LEFT JOIN ZICNOTEDATA ON ZICNOTEDATA.Z_PK = ico.ZNOTEDATA"
        b_sel  = ", ZICNOTEDATA.ZDATA AS ZBODYCONTENT"

    # Folder: parent entity in the same table
    f_sel = ""
    has_folder_col = "ZFOLDER" in ico_cols
    if has_folder_col:
        f_sel = ", folder.ZTITLE1 AS ZFOLDERNAME"

    folder_join = ""
    if has_folder_col:
        folder_join = "LEFT JOIN ZICCLOUDSYNCINGOBJECT folder ON folder.Z_PK = ico.ZFOLDER"

    sql = (
        f"SELECT ico.{t_col} AS ZTITLE, ico.ZCREATIONDATE AS ZCREATED,"
        f" ico.{m_col} AS ZMODIFIED {b_sel} {f_sel}"
        f" FROM ZICCLOUDSYNCINGOBJECT ico"
        f" {b_join} {folder_join}"
        f" WHERE ico.ZNOTEDATA IS NOT NULL"
        f"   AND (ico.ZISPASSWORDPROTECTED = 0 OR ico.ZISPASSWORDPROTECTED IS NULL)"
    )
    for row in con.execute(sql):
        keys = row.keys()
        notes.append(Note(
            title    = row["ZTITLE"] or "Untitled",
            body     = _decode_body(row["ZBODYCONTENT"] if "ZBODYCONTENT" in keys else None),
            created  = _apple_ts(row["ZCREATED"]),
            modified = _apple_ts(row["ZMODIFIED"]),
            folder   = row["ZFOLDERNAME"] if "ZFOLDERNAME" in keys else None,
        ))
    return notes


def _decode_body(raw) -> str:
    if not raw:
        return ""
    if isinstance(raw, memoryview):
        raw = bytes(raw)
    try:
        data = zlib.decompress(raw)
    except zlib.error:
        data = raw
    return _extract_text(data)


def _extract_text(data: bytes) -> str:
    texts: list[str] = []
    i, n = 0, len(data)
    while i < n:
        try:
            tag, i = _varint(data, i)
            wt = tag & 0x07
            if wt == 0:
                _, i = _varint(data, i)
            elif wt == 1:
                i += 8
            elif wt == 2:
                ln, i = _varint(data, i)
                if i + ln > n:
                    break
                chunk = data[i: i + ln]
                i += ln
                try:
                    text = chunk.decode("utf-8")
                    if len(text) >= 3 and _mostly_printable(text):
                        texts.append(text)
                except UnicodeDecodeError:
                    pass
            elif wt == 5:
                i += 4
            else:
                break
        except (IndexError, ValueError):
            break
    return "\n".join(texts)


def _varint(data: bytes, pos: int) -> tuple:
    result = shift = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift >= 64:
            raise ValueError("varint overflow")


def _mostly_printable(text: str) -> bool:
    ok = sum(1 for c in text if c.isprintable() or c in "\n\r\t")
    return ok / len(text) > 0.80


def _col_names(con, table: str) -> set:
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def _table_exists(con, table: str) -> bool:
    return bool(con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def _apple_ts(value) -> datetime | None:
    if value is None:
        return None
    try:
        return _APPLE_EPOCH + timedelta(seconds=float(value))
    except (TypeError, OverflowError, ValueError):
        return None
