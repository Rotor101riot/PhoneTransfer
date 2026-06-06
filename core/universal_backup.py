"""
universal_backup.py

PhoneTransfer Universal Backup archive (.ptbak).

A ZIP-based portable snapshot of an extracted iOS device, used as the
intermediate format for cross-platform restores (iOS → Android, iOS → iOS).

Archive layout
--------------
    metadata.json              — UDID, device name, iOS version, creation date,
                                 source platform, categories present
    {category}.json            — normalized item list for structured categories
    {category}/{filename}      — raw files for media categories, preserving
                                 the relative layout written by the extractor

Structured categories (serialized to JSON):
    contacts, blocked, sms, calls, calendar, reminders, notes, alarms,
    bookmarks, whatsapp, signal, health, apps

Media categories (files copied from staging directory):
    photos, videos, ringtones, voice_memos, wallpaper
"""

from __future__ import annotations

import json
import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_ARCHIVE_EXT = ".ptbak"

STRUCTURED_CATEGORIES: frozenset[str] = frozenset({
    "contacts", "blocked", "sms", "calls", "calendar", "reminders",
    "notes", "alarms", "bookmarks", "whatsapp", "signal", "health", "apps",
})

MEDIA_CATEGORIES: frozenset[str] = frozenset({
    "photos", "videos", "ringtones", "voice_memos", "wallpaper",
})


class BackupArchive:
    """
    Create and read a PhoneTransfer Universal Backup (.ptbak) archive.

    Parameters
    ----------
    path:
        Filesystem path for the archive file (created on ``create()``,
        must exist for ``load_metadata()`` / ``extract_to()``).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def create(
        self,
        staging_dir: Path,
        category_items: dict[str, list],
        metadata: dict,
    ) -> None:
        """
        Pack *category_items* and media files from *staging_dir* into the
        archive at ``self.path``.

        Parameters
        ----------
        staging_dir:
            Session staging directory; each media category has a sub-folder
            ``{staging_dir}/{category}/`` containing the extracted files.
        category_items:
            Mapping of category name → list of normalized items returned by
            the extractor.  Structured categories are serialized to JSON.
            Media categories use the files already present in *staging_dir*.
        metadata:
            Arbitrary dict written verbatim to ``metadata.json``.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        metadata = dict(metadata)
        metadata.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        metadata.setdefault("archive_version", 1)

        with zipfile.ZipFile(self.path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("metadata.json", json.dumps(metadata, indent=2, default=str))
            logger.debug("universal_backup: wrote metadata.json")

            for category, items in category_items.items():
                if category in STRUCTURED_CATEGORIES:
                    payload = json.dumps(items, indent=2, default=str)
                    zf.writestr(f"{category}.json", payload)
                    logger.debug(
                        "universal_backup: packed %s.json  (%d items)",
                        category, len(items),
                    )

                elif category in MEDIA_CATEGORIES:
                    cat_dir = staging_dir / category
                    if not cat_dir.is_dir():
                        logger.debug(
                            "universal_backup: no staging dir for '%s' — skipping",
                            category,
                        )
                        continue
                    file_count = 0
                    for fp in sorted(cat_dir.rglob("*")):
                        if fp.is_file():
                            arcname = f"{category}/{fp.relative_to(cat_dir)}"
                            zf.write(fp, arcname)
                            file_count += 1
                    logger.debug(
                        "universal_backup: packed %d file(s) for '%s'",
                        file_count, category,
                    )

                else:
                    logger.debug(
                        "universal_backup: unknown category '%s' — skipped", category
                    )

        size_mb = self.path.stat().st_size / (1024 * 1024)
        logger.info(
            "universal_backup: archive created → %s  (%.1f MB)", self.path, size_mb
        )

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def load_metadata(self) -> dict:
        """Return the ``metadata.json`` dict, or ``{}`` if absent."""
        with zipfile.ZipFile(self.path, "r") as zf:
            if "metadata.json" in zf.namelist():
                return json.loads(zf.read("metadata.json"))
        return {}

    def list_categories(self) -> list[str]:
        """Return sorted list of category names present in the archive."""
        cats: set[str] = set()
        with zipfile.ZipFile(self.path, "r") as zf:
            for name in zf.namelist():
                if name == "metadata.json":
                    continue
                if name.endswith(".json") and "/" not in name:
                    cats.add(name[:-5])
                elif "/" in name:
                    cats.add(name.split("/")[0])
        return sorted(cats)

    def extract_to(self, staging_dir: Path) -> dict[str, list]:
        """
        Extract the archive into *staging_dir*.

        Structured categories are deserialized from JSON and returned as
        ``{category: [items]}``.  Media categories have their files written
        to ``{staging_dir}/{category}/`` and return an empty list — injectors
        are expected to scan that directory directly.

        Returns
        -------
        dict mapping category name → normalized items list.
        """
        staging_dir.mkdir(parents=True, exist_ok=True)
        category_items: dict[str, list] = {}

        with zipfile.ZipFile(self.path, "r") as zf:
            for name in zf.namelist():
                if name == "metadata.json":
                    continue

                if name.endswith(".json") and "/" not in name:
                    category = name[:-5]
                    category_items[category] = json.loads(zf.read(name))
                    logger.debug(
                        "universal_backup: loaded %d items for '%s'",
                        len(category_items[category]), category,
                    )
                else:
                    # Media file — write to staging_dir, keeping directory structure
                    dest = staging_dir / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zf.read(name))
                    if "/" in name:
                        cat = name.split("/")[0]
                        category_items.setdefault(cat, [])

        return category_items


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def archive_path_for(udid: str, archive_dir: Path) -> Path:
    """
    Return the canonical archive path for *udid* inside *archive_dir*.

    Example: ``archive_dir / "00008110-001234567890001E_2026-03-20.ptbak"``
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return archive_dir / f"{udid}_{date_str}{_ARCHIVE_EXT}"
