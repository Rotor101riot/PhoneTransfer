"""
core/vault_reader.py

Deserialises a vault ZIP (written by vault_writer.py) back into lists of
normalised data objects that the standard injectors can consume directly.

Usage
-----
    from core.vault_reader import VaultReader
    from pathlib import Path

    reader = VaultReader(Path("backup_2026.zip"))
    manifest  = reader.manifest
    contacts  = reader.load_category("contacts")   # list[Contact]
    messages  = reader.load_category("sms")        # list[Message]
    photos    = reader.load_category("photos")     # list[MediaFile]
    reader.close()

Cross-ecosystem conversion
--------------------------
Because all vault data is already in the canonical normalization_schema
format, restoring to a different platform (e.g. iOS → Android or Android
→ iOS) is transparent — just pass the loaded lists to the appropriate
inject_{category}_{platform} function.
"""

from __future__ import annotations

import base64
import json
import logging
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.normalization_schema import (
    Alarm, BlockedNumber, Bookmark, BrowserHistoryEntry, CalendarEvent,
    CallRecord, Contact, HealthRecord, MediaFile, Message, MessageAttachment,
    Note, Reminder,
)
from core.vault_format import CATEGORY_FILENAMES as _CATEGORY_FILE, MANIFEST_FILENAME

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON decoder helpers
# ---------------------------------------------------------------------------

def _dt(val: str | None) -> datetime | None:
    """Parse an ISO 8601 string → timezone-aware datetime, or None."""
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(val.rstrip("Z"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _bytes(val: Any) -> bytes | None:
    """Decode a base64-encoded bytes dict produced by vault_writer._encode."""
    if isinstance(val, dict) and "__b64__" in val:
        try:
            return base64.b64decode(val["__b64__"])
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Per-type factory functions
# ---------------------------------------------------------------------------

def _contact(d: dict) -> Contact:
    return Contact(
        first_name   = d.get("first_name"),
        last_name    = d.get("last_name"),
        phones       = d.get("phones") or [],
        emails       = d.get("emails") or [],
        organization = d.get("organization"),
        note         = d.get("note"),
        raw_vcard    = d.get("raw_vcard"),
    )


def _attachment(d: dict, extract_dir: Path | None) -> MessageAttachment:
    data = _bytes(d.get("data"))
    local: Path | None = None
    if d.get("local_path") and extract_dir:
        candidate = extract_dir / d["local_path"]
        if candidate.exists():
            local = candidate
    return MessageAttachment(
        filename   = d.get("filename", "attachment"),
        mime_type  = d.get("mime_type", "application/octet-stream"),
        data       = data,
        local_path = local,
    )


def _message(d: dict, extract_dir: Path | None) -> Message:
    atts = [_attachment(a, extract_dir) for a in (d.get("attachments") or [])]
    return Message(
        platform_id = str(d.get("platform_id", "")),
        sender      = d.get("sender", ""),
        recipient   = d.get("recipient", ""),
        body        = d.get("body", ""),
        timestamp   = _dt(d.get("timestamp")) or datetime.now(timezone.utc),
        is_sent     = bool(d.get("is_sent", False)),
        attachments = atts,
        service     = d.get("service", "sms") if d.get("service") in ("sms", "mms", "imessage", "rcs") else "sms",
        read        = bool(d.get("read", True)),
    )


def _call(d: dict) -> CallRecord:
    return CallRecord(
        number           = d.get("number", ""),
        timestamp        = _dt(d.get("timestamp")) or datetime.now(timezone.utc),
        duration_seconds = int(d.get("duration_seconds", 0)),
        call_type        = d.get("call_type", "incoming"),
        name             = d.get("name"),
    )


def _calendar_event(d: dict) -> CalendarEvent:
    return CalendarEvent(
        title            = d.get("title", ""),
        start            = _dt(d.get("start")) or datetime.now(timezone.utc),
        end              = _dt(d.get("end"))   or datetime.now(timezone.utc),
        all_day          = bool(d.get("all_day", False)),
        uid              = d.get("uid"),
        location         = d.get("location"),
        notes            = d.get("notes"),
        recurrence_rule  = d.get("recurrence_rule"),
    )


def _note(d: dict) -> Note:
    return Note(
        title    = d.get("title", ""),
        body     = d.get("body", ""),
        created  = _dt(d.get("created")),
        modified = _dt(d.get("modified")),
        folder   = d.get("folder"),
    )


def _media_file(d: dict, extract_dir: Path | None) -> MediaFile | None:
    raw_path = d.get("local_path")
    if raw_path and extract_dir:
        local = extract_dir / raw_path
    elif raw_path:
        local = Path(str(raw_path))
    else:
        return None  # no binary — skip
    return MediaFile(
        filename   = d.get("filename", local.name),
        mime_type  = d.get("mime_type", "application/octet-stream"),
        local_path = local,
        created    = _dt(d.get("created")),
        album      = d.get("album"),
        latitude   = d.get("latitude"),
        longitude  = d.get("longitude"),
    )


def _blocked(d: dict) -> BlockedNumber:
    return BlockedNumber(
        number = d.get("number", ""),
        name   = d.get("name"),
    )


def _alarm(d: dict) -> Alarm:
    return Alarm(
        hour        = int(d.get("hour", 0)),
        minute      = int(d.get("minute", 0)),
        label       = d.get("label", ""),
        enabled     = bool(d.get("enabled", True)),
        repeat_days = d.get("repeat_days") or [],
        sound       = d.get("sound"),
    )


def _reminder(d: dict) -> Reminder:
    return Reminder(
        title     = d.get("title", ""),
        due       = _dt(d.get("due")),
        notes     = d.get("notes"),
        completed = bool(d.get("completed", False)),
        list_name = d.get("list_name"),
        uid       = d.get("uid"),
        priority  = int(d.get("priority", 0)),
    )


def _bookmark(d: dict) -> Bookmark:
    return Bookmark(
        title  = d.get("title", ""),
        url    = d.get("url", ""),
        folder = d.get("folder"),
        added  = _dt(d.get("added")),
    )


def _health_record(d: dict) -> HealthRecord:
    return HealthRecord(
        category    = d.get("category", ""),
        value       = float(d.get("value", 0.0)),
        unit        = d.get("unit", ""),
        start       = _dt(d.get("start")) or datetime.now(timezone.utc),
        end         = _dt(d.get("end")),
        source_name = d.get("source_name"),
        notes       = d.get("notes"),
    )


def _browser_entry(d: dict) -> BrowserHistoryEntry:
    return BrowserHistoryEntry(
        url         = d.get("url", ""),
        title       = d.get("title", ""),
        visited     = _dt(d.get("visited")) or datetime.now(timezone.utc),
        visit_count = int(d.get("visit_count", 1)),
        browser     = d.get("browser", "unknown"),
    )


def _app_info(d: dict, extract_dir: Path) -> dict:
    """
    Convert a vault-stored AppInfo dict back into a form the injector expects.

    The ``apk_files`` values are in-ZIP archive paths (e.g.
    ``media/apps/com.brave.browser/base_22.apk``).  Convert them to real
    filesystem paths under *extract_dir* where the APKs have been extracted.
    """
    result = dict(d)
    if "apk_files" in result and result["apk_files"]:
        result["apk_files"] = [
            extract_dir / p
            for p in result["apk_files"]
            if p
        ]
    return result


# ---------------------------------------------------------------------------
# VaultReader
# ---------------------------------------------------------------------------

class VaultReader:
    """
    Read-only view over a vault ZIP produced by :class:`VaultWriter`.

    Extracted media files are decompressed on demand into a temporary
    directory (``self.extract_dir``) that lives for the lifetime of this
    object.  Call :meth:`close` (or use as a context manager) to clean up.

    Parameters
    ----------
    vault_path:
        Path to the ``.zip`` vault file.
    """

    def __init__(self, vault_path: Path) -> None:
        self._vault_path = vault_path
        self._zf         = zipfile.ZipFile(vault_path, "r")
        self._tmpdir     = tempfile.TemporaryDirectory(prefix="pt_vault_")
        self.extract_dir = Path(self._tmpdir.name)
        self._manifest: dict | None = None
        logger.info("VaultReader: opened %s", vault_path)

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------

    @property
    def manifest(self) -> dict:
        """Return the vault manifest (cached after first access)."""
        if self._manifest is None:
            try:
                raw = self._zf.read(MANIFEST_FILENAME)
                self._manifest = json.loads(raw)
            except Exception as exc:
                logger.warning("VaultReader: could not read manifest: %s", exc)
                self._manifest = {}
        return self._manifest

    @property
    def available_categories(self) -> list[str]:
        """Categories that have data in this vault."""
        names_in_zip = {n for n in self._zf.namelist()}
        out = []
        for cat, fname in _CATEGORY_FILE.items():
            if fname in names_in_zip:
                out.append(cat)
        return out

    # ------------------------------------------------------------------
    # Category loading
    # ------------------------------------------------------------------

    def load_category(self, category: str) -> list:
        """
        Deserialise *category* from the vault and return a list of
        normalised objects ready for injection.

        For media categories, binary files are extracted to
        ``self.extract_dir`` so the returned ``MediaFile.local_path``
        values point to real files on disk.

        Returns an empty list if the category is not in the vault.
        """
        fname = _CATEGORY_FILE.get(category)
        if fname is None or fname not in self._zf.namelist():
            logger.debug("VaultReader: category '%s' not found in vault", category)
            return []

        try:
            raw  = self._zf.read(fname)
            data = json.loads(raw)
        except Exception as exc:
            logger.warning("VaultReader: failed to read %s: %s", fname, exc)
            return []

        # Extract any media files referenced in this category's JSON
        self._extract_media_for(category)

        return self._build_objects(category, data)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_media_for(self, category: str) -> None:
        """
        Extract all files from the ZIP that belong to this category's
        media subdirectory (e.g. media/photos/, media/attachments/).
        """
        prefixes = {
            "photos":      "media/photos/",
            "videos":      "media/videos/",
            "ringtones":   "media/ringtones/",
            "voice_memos": "media/voice_memos/",
            "wallpaper":   "media/wallpaper/",
            "sms":         "media/attachments/",
            "whatsapp":    "media/attachments/",
            "signal":      "media/attachments/",
            "apps":        "media/apps/",
        }
        prefix = prefixes.get(category)
        if not prefix:
            return

        for name in self._zf.namelist():
            if name.startswith(prefix) and not name.endswith("/"):
                dest = (self.extract_dir / name).resolve()
                # Guard against path traversal (e.g. "../../etc/passwd")
                if not str(dest).startswith(str(self.extract_dir.resolve())):
                    logger.warning("Skipping suspicious ZIP entry: %s", name)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    data = self._zf.read(name)
                    dest.write_bytes(data)

    def _build_objects(self, category: str, data: list[dict]) -> list:
        """Convert raw JSON dicts to normalised dataclass instances."""
        ed = self.extract_dir  # shorthand

        factories = {
            "contacts":    lambda d: _contact(d),
            "sms":         lambda d: _message(d, ed),
            "calls":       lambda d: _call(d),
            "calendar":    lambda d: _calendar_event(d),
            "notes":       lambda d: _note(d),
            "alarms":      lambda d: _alarm(d),
            "reminders":   lambda d: _reminder(d),
            "bookmarks":   lambda d: _bookmark(d),
            "blocked":     lambda d: _blocked(d),
            "whatsapp":    lambda d: _message(d, ed),
            "signal":      lambda d: _message(d, ed),
            "photos":      lambda d: _media_file(d, ed),
            "videos":      lambda d: _media_file(d, ed),
            "ringtones":   lambda d: _media_file(d, ed),
            "voice_memos": lambda d: _media_file(d, ed),
            "wallpaper":   lambda d: _media_file(d, ed),
            "health":      lambda d: _health_record(d),
            "browser":     lambda d: _browser_entry(d),
            "apps":        lambda d: _app_info(d, ed),
        }

        factory = factories.get(category)
        if factory is None:
            logger.warning("VaultReader: no factory for category '%s'", category)
            return []

        results = []
        for d in data:
            try:
                obj = factory(d)
                if obj is not None:
                    results.append(obj)
            except Exception as exc:
                logger.debug("VaultReader: skipping malformed record: %s", exc)
        return results

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release the ZIP handle and delete extracted temp files."""
        try:
            self._zf.close()
        except Exception:
            pass
        try:
            self._tmpdir.cleanup()
        except Exception:
            pass
        logger.info("VaultReader: closed %s", self._vault_path)

    def __enter__(self) -> "VaultReader":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
