"""
extract_notes_android.py

Extracts notes from an Android device connected via ADB.

Android has no universal notes system.  This module tries multiple sources in
priority order and merges the results, deduplicating by a hash of title+body.

Sources attempted (in order):
1. Samsung Notes content provider — works on Samsung devices without root.
2. Google Keep — no accessible content provider; skipped with a debug log.
3. Plain text files on /sdcard/Documents/ and /sdcard/Notes/ — pulled via
   `adb pull` and read locally.  Works without root.
4. ColorNote — popular third-party notes app; content provider attempted and
   silently skipped if not present.

For each source that yields data, Note objects are constructed and appended.
Deduplication runs after all sources are merged.

Never raises — all exceptions are caught, logged, and return partial/empty
results.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from core.normalization_schema import Note

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SUBDIR = "notes_android"

# Samsung Notes content provider URI and expected columns
_URI_SAMSUNG_NOTES = "content://com.samsung.android.app.notes.provider/notes"
_SAMSUNG_PROJECTION = "title:text:created_time:modified_time"

# ColorNote (Pro) content provider URI
_URI_COLORNOTE = (
    "content://com.socialnmobile.dict.colornotepro.provider.Note/notes"
)
_COLORNOTE_PROJECTION = "title:note:created_date:modified_date"

# Plain text file locations on /sdcard
_SDCARD_SCAN_DIRS = ["/sdcard/Documents", "/sdcard/Notes"]

# Maximum file size pulled from device for plain-text notes (bytes)
_MAX_TXT_SIZE = 512 * 1024  # 512 KB


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(device_id: str, staging_dir: Path, is_rooted: bool) -> list[Note]:
    """
    Extract notes from the Android device identified by *device_id*.

    Parameters
    ----------
    device_id:   ADB device serial string.
    staging_dir: Root staging directory for this transfer session.
    is_rooted:   Accepted for interface consistency; currently no extra paths
                 are unlocked by root for notes extraction.

    Returns
    -------
    list[Note]   Possibly empty on total failure.
    """
    try:
        return _extract_impl(device_id, staging_dir, is_rooted)
    except Exception as exc:
        logger.exception(
            "[notes/android] Top-level failure for %s: %s", device_id, exc
        )
        return []


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------

def _extract_impl(
    device_id: str, staging_dir: Path, is_rooted: bool
) -> list[Note]:
    from core.adb_manager import ADBManager
    from core.config_loader import get_config

    sub = staging_dir / _SUBDIR
    sub.mkdir(parents=True, exist_ok=True)

    adb = ADBManager(get_config())
    all_notes: list[Note] = []

    # ------------------------------------------------------------------
    # Source 1: Samsung Notes
    # ------------------------------------------------------------------
    samsung_notes = _extract_samsung_notes(device_id, adb)
    if samsung_notes:
        logger.info(
            "[notes/android] Samsung Notes: %d note(s) from %s",
            len(samsung_notes),
            device_id,
        )
        all_notes.extend(samsung_notes)
    else:
        logger.debug(
            "[notes/android] Samsung Notes: no results (non-Samsung device or "
            "provider unavailable)"
        )

    # ------------------------------------------------------------------
    # Source 2: Google Keep — no accessible content provider without root
    # ------------------------------------------------------------------
    logger.debug(
        "[notes/android] Google Keep: no accessible content provider without "
        "root — skipping."
    )

    # ------------------------------------------------------------------
    # Source 3: Plain .txt files on /sdcard
    # ------------------------------------------------------------------
    txt_notes = _extract_txt_files(device_id, sub, adb)
    if txt_notes:
        logger.info(
            "[notes/android] Plain TXT files: %d note(s) from %s",
            len(txt_notes),
            device_id,
        )
        all_notes.extend(txt_notes)

    # ------------------------------------------------------------------
    # Source 4: ColorNote
    # ------------------------------------------------------------------
    color_notes = _extract_colornote(device_id, adb)
    if color_notes:
        logger.info(
            "[notes/android] ColorNote: %d note(s) from %s",
            len(color_notes),
            device_id,
        )
        all_notes.extend(color_notes)
    else:
        logger.debug(
            "[notes/android] ColorNote: provider unavailable or no notes."
        )

    # ------------------------------------------------------------------
    # Deduplicate by title+body hash
    # ------------------------------------------------------------------
    deduplicated = _deduplicate(all_notes)
    logger.info(
        "[notes/android] Total after dedup: %d note(s) for %s",
        len(deduplicated),
        device_id,
    )
    return deduplicated


# ---------------------------------------------------------------------------
# Source 1: Samsung Notes
# ---------------------------------------------------------------------------

def _extract_samsung_notes(device_id: str, adb) -> list[Note]:
    """Query Samsung Notes content provider."""
    stdout, stderr, rc = adb.shell(
        device_id,
        f"content query --uri {_URI_SAMSUNG_NOTES} "
        f"--projection {_SAMSUNG_PROJECTION}",
        timeout=30,
    )
    if rc != 0 or not stdout.strip():
        logger.debug(
            "[notes/android] Samsung Notes query failed (rc=%d): %s",
            rc,
            stderr.strip(),
        )
        return []

    notes: list[Note] = []
    for row in _parse_content_rows(stdout):
        try:
            note = _samsung_row_to_note(row)
            if note is not None:
                notes.append(note)
        except Exception as exc:
            logger.debug("[notes/android] Samsung row parse error: %s", exc)

    return notes


def _samsung_row_to_note(row: dict[str, str]) -> Note | None:
    title = (row.get("title") or "").strip() or "Untitled"
    body  = (row.get("text")  or "").strip()

    created_ms  = row.get("created_time",  "")
    modified_ms = row.get("modified_time", "")

    return Note(
        title    = title,
        body     = body,
        created  = _ms_to_dt(created_ms),
        modified = _ms_to_dt(modified_ms),
        folder   = "Samsung Notes",
    )


# ---------------------------------------------------------------------------
# Source 3: Plain .txt files on /sdcard
# ---------------------------------------------------------------------------

def _extract_txt_files(device_id: str, sub: Path, adb) -> list[Note]:
    """Find and pull .txt files from common /sdcard locations."""
    # Discover file paths via find
    scan_targets = " ".join(_SDCARD_SCAN_DIRS)
    stdout, _, rc = adb.shell(
        device_id,
        f"find {scan_targets} -name '*.txt' -type f 2>/dev/null",
        timeout=30,
    )
    if rc != 0 or not stdout.strip():
        logger.debug("[notes/android] No .txt files found in /sdcard locations.")
        return []

    remote_paths = [p.strip() for p in stdout.splitlines() if p.strip()]
    if not remote_paths:
        return []

    logger.debug(
        "[notes/android] Found %d .txt file(s) on device.", len(remote_paths)
    )

    txt_dir = sub / "txt_files"
    txt_dir.mkdir(parents=True, exist_ok=True)

    notes: list[Note] = []
    for remote_path in remote_paths:
        try:
            note = _pull_and_read_txt(device_id, remote_path, txt_dir, adb)
            if note is not None:
                notes.append(note)
        except Exception as exc:
            logger.debug(
                "[notes/android] Failed to pull %s: %s", remote_path, exc
            )

    return notes


def _pull_and_read_txt(
    device_id: str, remote_path: str, dest_dir: Path, adb
) -> Note | None:
    """Pull a single .txt file and construct a Note from it."""
    # Use the remote filename as the local filename (sanitised)
    remote_name = remote_path.rsplit("/", 1)[-1]
    local_path  = dest_dir / remote_name

    # Avoid clobbering if two remote files share the same name
    if local_path.exists():
        stem   = local_path.stem
        suffix = local_path.suffix
        idx    = 1
        while local_path.exists():
            idx += 1
            local_path = dest_dir / f"{stem}_{idx}{suffix}"

    ok = adb.pull(device_id, remote_path, local_path, timeout=30)
    if not ok or not local_path.exists():
        return None

    # Reject very large files to avoid flooding memory
    if local_path.stat().st_size > _MAX_TXT_SIZE:
        logger.debug(
            "[notes/android] Skipping oversized file: %s (%d bytes)",
            remote_path,
            local_path.stat().st_size,
        )
        return None

    body = _read_file_safe(local_path)
    title = local_path.stem  # filename without extension becomes the title

    return Note(
        title    = title,
        body     = body,
        created  = None,
        modified = None,
        folder   = "Files",
    )


def _read_file_safe(path: Path) -> str:
    """Read a text file, trying UTF-8 then Latin-1 as fallback."""
    for enc in ("utf-8", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, OSError):
            continue
    return ""


# ---------------------------------------------------------------------------
# Source 4: ColorNote
# ---------------------------------------------------------------------------

def _extract_colornote(device_id: str, adb) -> list[Note]:
    """Query ColorNote content provider.  Silently returns [] on any failure."""
    stdout, stderr, rc = adb.shell(
        device_id,
        f"content query --uri {_URI_COLORNOTE} "
        f"--projection {_COLORNOTE_PROJECTION}",
        timeout=30,
    )
    if rc != 0 or not stdout.strip():
        logger.debug(
            "[notes/android] ColorNote query failed (rc=%d): %s",
            rc,
            stderr.strip(),
        )
        return []

    notes: list[Note] = []
    for row in _parse_content_rows(stdout):
        try:
            note = _colornote_row_to_note(row)
            if note is not None:
                notes.append(note)
        except Exception as exc:
            logger.debug("[notes/android] ColorNote row parse error: %s", exc)

    return notes


def _colornote_row_to_note(row: dict[str, str]) -> Note | None:
    title = (row.get("title") or "").strip() or "Untitled"
    body  = (row.get("note")  or "").strip()

    created_ms  = row.get("created_date",  "")
    modified_ms = row.get("modified_date", "")

    return Note(
        title    = title,
        body     = body,
        created  = _ms_to_dt(created_ms),
        modified = _ms_to_dt(modified_ms),
        folder   = "ColorNote",
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _parse_content_rows(output: str) -> list[dict[str, str]]:
    """
    Parse the stdout of `adb shell content query` into a list of dicts.
    Splits only at ", word=" boundaries to preserve commas inside values.
    """
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("Row:"):
            continue
        _, _, rest = line.partition(" ")    # drop "Row:"
        _, _, rest = rest.partition(" ")    # drop row index
        rest = rest.strip()
        if not rest:
            continue
        pairs = re.split(r",\s+(?=\w+=)", rest)
        row: dict[str, str] = {}
        for pair in pairs:
            k, _, v = pair.partition("=")
            row[k.strip()] = v.strip()
        rows.append(row)
    return rows


def _ms_to_dt(ms_str: str) -> datetime | None:
    """Convert a Unix milliseconds string to a UTC-aware datetime."""
    if not ms_str:
        return None
    try:
        ms = int(ms_str)
        if ms == 0:
            return None
        return datetime.utcfromtimestamp(ms / 1000.0).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _note_hash(note: Note) -> str:
    """Compute a deduplication key from title and body."""
    raw = f"{note.title}\x00{note.body}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _deduplicate(notes: list[Note]) -> list[Note]:
    """Remove duplicate notes by title+body hash.  Preserves first occurrence."""
    seen: set[str] = set()
    result: list[Note] = []
    for note in notes:
        h = _note_hash(note)
        if h not in seen:
            seen.add(h)
            result.append(note)
    return result
