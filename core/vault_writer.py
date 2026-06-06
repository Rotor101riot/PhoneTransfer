"""
core/vault_writer.py

Serializes normalized extraction results into a self-contained "vault" ZIP —
the universal backup format that can be restored to any supported platform
(iOS or Android) via vault_reader.py + the appropriate injectors.

Vault ZIP layout
----------------
  manifest.json          — metadata (source device, date, version, counts)
  contacts.json          — list[Contact]
  sms.json               — list[Message]
  calls.json             — list[CallRecord]
  calendar.json          — list[CalendarEvent]
  notes.json             — list[Note]
  alarms.json            — list[Alarm]
  reminders.json         — list[Reminder]
  bookmarks.json         — list[Bookmark]
  blocked.json           — list[BlockedNumber]
  whatsapp.json          — list[Message]  (WhatsApp chats)
  signal.json            — list[Message]  (Signal messages)
  media/photos/          — photo binaries
  media/videos/          — video binaries
  media/ringtones/       — ringtone binaries
  media/voice_memos/     — voice memo binaries
  media/attachments/     — SMS/WhatsApp/Signal attachment binaries

Usage
-----
    from core.vault_writer import VaultWriter
    from pathlib import Path

    writer = VaultWriter(output_path=Path("backup_2026.zip"), source_device=dev)
    writer.add_category("contacts", contacts_list)
    writer.add_category("sms", messages_list)
    writer.add_category("photos", media_list)
    writer.finalise()          # writes manifest and closes ZIP
"""

from __future__ import annotations

import base64
import json
import logging
import zipfile
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.normalization_schema import DeviceInfo
from core.vault_format import (
    VAULT_FORMAT_VERSION,
    MANIFEST_FILENAME,
    CATEGORY_FILENAMES as _CATEGORY_FILE,
    MEDIA_SUBDIRS      as _MEDIA_SUBDIR,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON encoder
# ---------------------------------------------------------------------------

def _encode(obj: Any) -> Any:
    """Custom JSON encoder hook for types not natively supported."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, bytes):
        return {"__b64__": base64.b64encode(obj).decode()}
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serialisable")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=_encode, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# VaultWriter
# ---------------------------------------------------------------------------

class VaultWriter:
    """
    Incrementally writes normalised data into a vault ZIP file.

    Instantiate, call :meth:`add_category` for each category that was
    extracted, then call :meth:`finalise` once to close the archive.

    Parameters
    ----------
    output_path:
        Destination path for the vault ZIP (created or overwritten).
    source_device:
        DeviceInfo of the phone that was backed up.  Written into
        ``manifest.json`` for provenance.
    """

    def __init__(self, output_path: Path, source_device: DeviceInfo) -> None:
        self._output_path = output_path
        self._source      = source_device
        self._counts: dict[str, int] = {}
        self._zf = zipfile.ZipFile(
            output_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        )
        logger.info("VaultWriter: opened %s", output_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_category(self, category: str, items: list) -> int:
        """
        Serialise *items* (a list of normalised objects) into the vault.

        For media categories (photos, videos, ringtones, voice_memos,
        wallpaper) the actual binary files are copied into the appropriate
        ``media/`` subdirectory.  For attachment-bearing message types
        (sms, whatsapp, signal) attachment binaries are written to
        ``media/attachments/``.

        Returns the number of items written.
        """
        if not items:
            self._counts[category] = 0
            return 0

        json_filename = _CATEGORY_FILE.get(category, f"{category}.json")
        media_subdir  = _MEDIA_SUBDIR.get(category, "")

        serialised = self._serialise_items(items, media_subdir)
        self._zf.writestr(json_filename, _dumps(serialised))

        n = len(serialised)
        self._counts[category] = n
        logger.debug("VaultWriter: %s → %d items → %s", category, n, json_filename)
        return n

    def finalise(self) -> None:
        """Write the manifest and close the ZIP.  Must be called exactly once."""
        manifest = {
            "vault_version":   VAULT_FORMAT_VERSION,
            "created_at":      datetime.now(timezone.utc).isoformat(),
            "source_platform": self._source.platform,
            "source_model":    self._source.model,
            "source_name":     self._source.name,
            "source_os":       self._source.os_version,
            "source_udid":     self._source.udid,
            "item_counts":     self._counts,
            "total_items":     sum(self._counts.values()),
        }
        self._zf.writestr(MANIFEST_FILENAME, _dumps(manifest))
        self._zf.close()
        size_mb = self._output_path.stat().st_size / (1024 * 1024)
        logger.info(
            "VaultWriter: finalised %s (%.1f MB, %d total items)",
            self._output_path.name, size_mb, manifest["total_items"],
        )

    def __enter__(self) -> "VaultWriter":
        return self

    def __exit__(self, *_: Any) -> None:
        try:
            self.finalise()
        except Exception as exc:
            logger.debug("VaultWriter: error during finalise in __exit__: %s", exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _serialise_items(self, items: list, media_subdir: str) -> list[dict]:
        """
        Convert a list of normalised dataclass instances to JSON-ready dicts,
        copying any referenced binary files into the ZIP along the way.
        """
        result = []
        seen_names: dict[str, int] = {}  # deduplicate filenames within a subdir

        for item in items:
            # Support both dataclass instances (the norm) and plain dicts
            # (e.g. AppInfo dicts from extract_apps_android).
            d = asdict(item) if is_dataclass(item) else dict(item)

            # --- AppInfo: copy apk_files list into media/apps/<pkg>/ ---------
            if "apk_files" in d and d["apk_files"]:
                pkg = d.get("package", "unknown_app")
                app_subdir = f"media/apps/{pkg}"
                arc_paths = []
                for apk_path in d["apk_files"]:
                    arc = self._copy_file(Path(str(apk_path)), app_subdir, seen_names)
                    if arc:
                        arc_paths.append(arc)
                d["apk_files"] = arc_paths

            # --- MediaFile: copy local_path file into the ZIP ----------------
            if "local_path" in d and d["local_path"] is not None:
                arc = self._copy_file(
                    Path(str(d["local_path"])),
                    media_subdir or "media/misc",
                    seen_names,
                )
                d["local_path"] = arc  # store the in-ZIP path

            # --- MessageAttachment: copy data/local_path into media/attachments
            if "attachments" in d:
                for att in d["attachments"]:
                    if att.get("local_path") is not None:
                        arc = self._copy_file(
                            Path(str(att["local_path"])),
                            "media/attachments",
                            seen_names,
                        )
                        att["local_path"] = arc
                    # bytes data is kept inline (base64-encoded by _encode)

            result.append(d)
        return result

    def _copy_file(
        self,
        src: Path,
        zip_subdir: str,
        seen: dict[str, int],
    ) -> str | None:
        """
        Copy *src* into *zip_subdir* inside the ZIP, deduplicating filenames.
        Returns the in-ZIP arcname, or None if the file doesn't exist.
        """
        if not src.exists():
            logger.debug("VaultWriter: source file missing, skipping: %s", src)
            return None

        stem, suffix = src.stem, src.suffix
        base_name    = src.name
        count        = seen.get(base_name, 0)
        if count:
            base_name = f"{stem}_{count}{suffix}"
        seen[src.name] = count + 1

        arcname = f"{zip_subdir}/{base_name}"
        try:
            self._zf.write(src, arcname)
        except Exception as exc:
            logger.warning("VaultWriter: failed to write %s: %s", src, exc)
            return None
        return arcname
