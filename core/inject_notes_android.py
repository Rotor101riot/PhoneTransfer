"""
inject_notes_android.py

Injects Note records into an Android device connected via ADB by pushing
plain text files to /sdcard/Documents/PhoneTransfer/notes/.

Procedure
---------
1. Create a local temporary directory at staging_dir/notes_android_inject/.
2. For each Note, write a UTF-8 .txt file.  The filename is derived from the
   note title: truncated to 100 characters, with the characters /\\:*?"<>|
   replaced by underscores.
3. Handle filename collisions by appending _2, _3, etc.
4. ADB-push each file to /sdcard/Documents/PhoneTransfer/notes/<filename>.
5. After all files are pushed, broadcast ACTION_MEDIA_SCANNER_SCAN_FILE so
   the MediaStore picks up the new files.
6. Return the count of successfully pushed files.

Never raises — all exceptions are caught, logged, and cause the affected note
to be counted as a failure (not a crash).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from core.normalization_schema import Note

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REMOTE_NOTES_DIR   = "/sdcard/Documents/PhoneTransfer/notes"
_LOCAL_STAGING_NAME = "notes_android_inject"
_FILENAME_MAX_LEN   = 100                # characters, before .txt extension
_INVALID_CHARS_RE   = re.compile(r'[/\\:*?"<>|]')


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def inject(
    device_id: str,
    items: list[Note],
    staging_dir: Path,
    is_rooted: bool,
) -> int:
    """
    Inject notes into the Android device identified by *device_id*.

    Parameters
    ----------
    device_id:   ADB device serial string.
    items:       Note objects to inject.
    staging_dir: Root staging directory for this transfer session.
    is_rooted:   Accepted for interface consistency; the same push strategy
                 is used regardless of root status.

    Returns
    -------
    int: Count of note files successfully pushed to the device.
         Returns 0 on total failure.
    """
    try:
        return _inject_impl(device_id, items, staging_dir)
    except Exception as exc:
        logger.exception(
            "[notes_inject/android] Top-level failure for %s: %s",
            device_id,
            exc,
        )
        return 0


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------

def _inject_impl(
    device_id: str, items: list[Note], staging_dir: Path
) -> int:
    from core.adb_manager import ADBManager
    from core.config_loader import get_config

    if not items:
        logger.info("[notes_inject/android] No notes to inject — done.")
        return 0

    logger.info(
        "[notes_inject/android] Injecting %d note(s) to %s",
        len(items),
        device_id,
    )

    # ------------------------------------------------------------------
    # Step 1: Prepare local staging directory
    # ------------------------------------------------------------------
    local_dir = staging_dir / _LOCAL_STAGING_NAME
    try:
        local_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.error(
            "[notes_inject/android] Cannot create local staging dir %s: %s",
            local_dir,
            exc,
        )
        return 0

    # ------------------------------------------------------------------
    # Step 2 + 3: Write .txt files with collision-safe filenames
    # ------------------------------------------------------------------
    local_files: list[tuple[Path, str]] = []  # (local_path, remote_filename)
    used_names: set[str] = set()

    for i, note in enumerate(items):
        try:
            filename = _make_filename(note.title, used_names)
            used_names.add(filename)
            local_path = local_dir / filename
            _write_note_file(local_path, note)
            local_files.append((local_path, filename))
        except Exception as exc:
            logger.warning(
                "[notes_inject/android] Failed to write note %d (%r): %s",
                i,
                note.title,
                exc,
            )

    if not local_files:
        logger.error(
            "[notes_inject/android] No note files could be written locally."
        )
        return 0

    # ------------------------------------------------------------------
    # Step 4: ADB push each file
    # ------------------------------------------------------------------
    adb = ADBManager(get_config())

    # Ensure remote directory exists
    adb.shell(device_id, f"mkdir -p {_REMOTE_NOTES_DIR}", timeout=10)

    success = 0
    for local_path, filename in local_files:
        remote_path = f"{_REMOTE_NOTES_DIR}/{filename}"
        ok = adb.push(device_id, local_path, remote_path, timeout=30)
        if ok:
            success += 1
            logger.debug(
                "[notes_inject/android] Pushed: %s -> %s",
                local_path.name,
                remote_path,
            )
        else:
            logger.warning(
                "[notes_inject/android] Push failed: %s -> %s",
                local_path.name,
                remote_path,
            )

    logger.info(
        "[notes_inject/android] Pushed %d/%d note file(s).",
        success,
        len(local_files),
    )

    # ------------------------------------------------------------------
    # Step 5: Trigger MediaScanner so files appear in the MediaStore
    # ------------------------------------------------------------------
    if success > 0:
        _trigger_media_scanner(device_id, adb)

    return success


# ---------------------------------------------------------------------------
# Filename handling
# ---------------------------------------------------------------------------

def _sanitize_title(title: str) -> str:
    """
    Convert a note title into a safe filename stem.
    Replaces forbidden characters with underscores and strips leading/trailing
    whitespace and dots.  Truncates to _FILENAME_MAX_LEN characters.
    """
    sanitized = _INVALID_CHARS_RE.sub("_", title)
    sanitized = sanitized.strip(". ")
    sanitized = sanitized[:_FILENAME_MAX_LEN]
    if not sanitized:
        sanitized = "note"
    return sanitized


def _make_filename(title: str, used: set[str]) -> str:
    """
    Build a collision-safe .txt filename from *title*.
    If the candidate name is already in *used*, appends _2, _3, etc.
    Does NOT add the new name to *used* — the caller is responsible.
    """
    stem = _sanitize_title(title)
    candidate = f"{stem}.txt"
    if candidate not in used:
        return candidate

    idx = 2
    while True:
        candidate = f"{stem}_{idx}.txt"
        if candidate not in used:
            return candidate
        idx += 1


# ---------------------------------------------------------------------------
# File writing
# ---------------------------------------------------------------------------

def _write_note_file(path: Path, note: Note) -> None:
    """
    Write a Note to a local .txt file.

    Format:
        <title>
        <blank line>
        <body>
    """
    content_parts = [note.title]
    if note.body:
        content_parts.append("")          # blank separator line
        content_parts.append(note.body)
    content = "\n".join(content_parts)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# MediaScanner broadcast
# ---------------------------------------------------------------------------

def _trigger_media_scanner(device_id: str, adb) -> None:
    """
    Broadcast ACTION_MEDIA_SCANNER_SCAN_FILE for the notes directory so the
    system MediaStore indexes the newly pushed files.
    """
    cmd = (
        "am broadcast "
        "-a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
        f"-d file:///{_REMOTE_NOTES_DIR}/"
    )
    _, stderr, rc = adb.shell(device_id, cmd, timeout=15)
    if rc == 0:
        logger.debug(
            "[notes_inject/android] MediaScanner broadcast sent for %s",
            _REMOTE_NOTES_DIR,
        )
    else:
        logger.warning(
            "[notes_inject/android] MediaScanner broadcast failed (rc=%d): %s",
            rc,
            stderr.strip(),
        )
