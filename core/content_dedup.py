"""
content_dedup.py

Hash-based content deduplication across transfer sessions.

Problem
-------
When transferring data between the same device pair multiple times (e.g.
incremental transfers, re-runs after partial failure), the pipeline
re-transfers items that already exist on the destination.  For large media
categories (photos, videos) this wastes significant time and bandwidth.

Solution
--------
A persistent content fingerprint store that records which items have been
successfully transferred.  Before injecting, the pipeline can call
``filter_duplicates()`` to strip items that were already transferred in a
prior session.

Fingerprinting strategy:
- **Structured data** (contacts, messages, calls, etc.): SHA-256 of a
  canonical JSON serialisation of key fields.
- **Media files**: SHA-256 of the first 64 KB + file size.  Full-file
  hashing is too slow for large videos; the prefix + size combination
  catches >99.9% of duplicates with negligible collision risk.

Storage
-------
The dedup store is a JSON file per device-pair, stored under the
PhoneTransfer data directory:

    <data_dir>/dedup/<src_serial>_to_<dst_serial>.json

The store maps ``category → {fingerprint → transfer_timestamp}``.

Usage
-----
    from core.content_dedup import DedupStore

    store = DedupStore(src_serial="ABC123", dst_serial="DEF456")
    unique_contacts = store.filter_duplicates("contacts", all_contacts)
    # ... inject unique_contacts ...
    store.mark_transferred("contacts", unique_contacts)
    store.save()
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# On-disk format version.  Bump this whenever the JSON structure changes in a
# way that is incompatible with older readers (not when only adding categories
# or new fingerprint keys).  _load() resets the store on unknown versions so
# the pipeline never silently reads stale/wrong data.
_SCHEMA_VERSION = 1

# Size of the file prefix used for media fingerprinting (64 KB)
_MEDIA_PREFIX_BYTES = 64 * 1024

# Categories whose items are media files (fingerprinted by file content)
_MEDIA_CATEGORIES = frozenset({
    "photos", "videos", "ringtones", "voice_memos", "wallpaper",
})

# Per-category key fields used for structural fingerprinting.
# Only these fields are hashed — transient fields like local_path or
# staging artifacts are excluded to ensure stable fingerprints across runs.
_FINGERPRINT_KEYS: dict[str, list[str]] = {
    "contacts":       ["first_name", "last_name", "phones", "emails", "organization"],
    "sms":            ["platform_id", "sender", "recipient", "body", "timestamp", "service"],
    "calls":          ["number", "call_type", "timestamp", "duration_seconds"],
    "calendar":       ["uid", "title", "start", "end"],
    "notes":          ["title", "body", "created"],
    "reminders":      ["title", "due", "uid"],
    "bookmarks":      ["title", "url"],
    "blocked":        ["number"],
    "alarms":         ["hour", "minute", "label", "repeat_days"],
    "contact_groups": ["title", "group_id"],
    "browser_history": ["url", "title"],
    "clipboard":      ["text"],
    "installed_apps": ["package_name", "version_name", "version_code"],
    "whatsapp":       ["platform_id", "sender", "recipient", "body", "timestamp"],
    "telegram":       ["platform_id", "sender", "recipient", "body", "timestamp"],
}


def _default_data_dir() -> Path:
    """Return the default PhoneTransfer data directory."""
    try:
        from core.config_loader import get_config
        cfg = get_config()
        return Path(getattr(cfg, "data_dir", "tmp"))
    except Exception:
        return Path("tmp")


class DedupStore:
    """
    Persistent fingerprint store for cross-session deduplication.

    Parameters
    ----------
    src_serial:
        Source device serial / UDID.
    dst_serial:
        Destination device serial / UDID.
    data_dir:
        Override for the store directory.  Defaults to ``<data_dir>/dedup/``.
    """

    def __init__(
        self,
        src_serial: str,
        dst_serial: str,
        data_dir: Path | None = None,
    ) -> None:
        self._src = src_serial
        self._dst = dst_serial

        base = data_dir or _default_data_dir()
        self._store_dir = base / "dedup"
        self._store_dir.mkdir(parents=True, exist_ok=True)

        safe_src = src_serial.replace("/", "_").replace("\\", "_")
        safe_dst = dst_serial.replace("/", "_").replace("\\", "_")
        self._path = self._store_dir / f"{safe_src}_to_{safe_dst}.json"

        self._data: dict[str, dict[str, str]] = {}
        # id(item) → fingerprint string.  Populated by filter_duplicates() so
        # mark_transferred() can reuse them without re-reading/re-hashing files.
        # Object IDs are safe here because items remain alive between the two calls.
        self._fp_cache: dict[int, str] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)

            # v0 (legacy): flat dict of category → {fp → timestamp}
            # v1+: {"_schema_version": N, "categories": {...}}
            if isinstance(raw, dict) and "_schema_version" not in raw:
                # Migrate v0 → v1 in memory (will be written as v1 on next save)
                logger.debug(
                    "dedup: migrating %s from v0 (flat) to v%d",
                    self._path, _SCHEMA_VERSION,
                )
                self._data = raw
            elif isinstance(raw, dict):
                file_version = int(raw.get("_schema_version", 0))
                if file_version == _SCHEMA_VERSION:
                    self._data = raw.get("categories", {})
                elif file_version < _SCHEMA_VERSION:
                    # Future migration hook: add per-version upgrade steps here.
                    logger.warning(
                        "dedup: %s is schema v%d; current is v%d — resetting store",
                        self._path, file_version, _SCHEMA_VERSION,
                    )
                    self._data = {}
                else:
                    logger.warning(
                        "dedup: %s is schema v%d which is newer than supported v%d "
                        "— resetting store to avoid corrupt reads",
                        self._path, file_version, _SCHEMA_VERSION,
                    )
                    self._data = {}
            else:
                logger.warning(
                    "dedup: unexpected format in %s — resetting store", self._path
                )
                self._data = {}

            logger.debug(
                "dedup: loaded %d categories from %s", len(self._data), self._path
            )
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning("dedup: failed to load %s: %s", self._path, exc)
            self._data = {}

    def save(self) -> None:
        """Persist the store to disk in the versioned envelope format."""
        try:
            envelope = {
                "_schema_version": _SCHEMA_VERSION,
                "categories": self._data,
            }
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(envelope, fh, indent=1)
            logger.debug("dedup: saved to %s", self._path)
        except OSError as exc:
            logger.warning("dedup: failed to save %s: %s", self._path, exc)

    # ------------------------------------------------------------------
    # Fingerprinting
    # ------------------------------------------------------------------

    @staticmethod
    def fingerprint_item(category: str, item: Any) -> str | None:
        """
        Compute a SHA-256 fingerprint for a single item.

        Returns None if the item cannot be fingerprinted (e.g. missing
        required data).
        """
        if category in _MEDIA_CATEGORIES:
            return _fingerprint_media(item)
        return _fingerprint_structured(category, item)

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def filter_duplicates(self, category: str, items: list) -> list:
        """
        Return only items whose fingerprints are NOT already in the store.

        Items that cannot be fingerprinted are always included (safe side).
        Fingerprints computed here are cached so ``mark_transferred()`` can
        reuse them without re-reading or re-hashing media files.
        """
        known = self._data.get(category, {})

        unique = []
        skipped = 0
        for item in items:
            fp = self.fingerprint_item(category, item)
            if fp is not None:
                self._fp_cache[id(item)] = fp  # cache for mark_transferred
            if fp is None or fp not in known:
                unique.append(item)
            else:
                skipped += 1

        if skipped:
            logger.info(
                "dedup: %s — skipped %d duplicate(s), %d unique remaining",
                category, skipped, len(unique),
            )
        return unique

    def mark_transferred(self, category: str, items: list) -> None:
        """
        Record fingerprints of successfully transferred items.

        Call this *after* injection succeeds so only confirmed transfers
        are cached.  Reuses fingerprints computed by ``filter_duplicates()``
        when available to avoid re-reading media files.
        """
        cat_store = self._data.setdefault(category, {})
        now = datetime.now().isoformat()

        added = 0
        for item in items:
            # Prefer cached fingerprint computed during filter_duplicates()
            fp = self._fp_cache.get(id(item)) or self.fingerprint_item(category, item)
            if fp is not None:
                cat_store[fp] = now
                added += 1

        if added:
            logger.debug("dedup: marked %d items transferred for %s", added, category)

    def is_duplicate(self, category: str, item: Any) -> bool:
        """Check if a single item is already in the store."""
        fp = self.fingerprint_item(category, item)
        if fp is None:
            return False
        return fp in self._data.get(category, {})

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def clear_category(self, category: str) -> None:
        """Remove all fingerprints for a category."""
        self._data.pop(category, None)
        self._fp_cache.clear()

    def clear_all(self) -> None:
        """Remove all fingerprints."""
        self._data.clear()
        self._fp_cache.clear()

    @property
    def stats(self) -> dict[str, int]:
        """Return per-category fingerprint counts."""
        return {cat: len(fps) for cat, fps in self._data.items()}


# ---------------------------------------------------------------------------
# Fingerprinting implementations
# ---------------------------------------------------------------------------

def _fingerprint_structured(category: str, item: Any) -> str | None:
    """
    SHA-256 of a canonical JSON serialisation of key fields.

    Uses _FINGERPRINT_KEYS to select only stable, identifying fields.
    """
    keys = _FINGERPRINT_KEYS.get(category)
    if keys is None:
        # Unknown category — try hashing all fields via asdict
        try:
            blob = json.dumps(asdict(item), sort_keys=True, default=str)
            return hashlib.sha256(blob.encode()).hexdigest()
        except Exception:
            return None

    data = {}
    for k in keys:
        val = getattr(item, k, None)
        if val is None:
            val = ""
        data[k] = val

    try:
        blob = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()
    except Exception:
        return None


def _fingerprint_media(item: Any) -> str | None:
    """
    SHA-256 of the first 64 KB + file size for media files.

    Falls back to filename + mime_type if the file is not accessible.
    """
    local_path = getattr(item, "local_path", None)
    if local_path is None:
        return None

    p = Path(str(local_path))
    if not p.exists():
        # File not staged yet — use filename + mime_type as weak fingerprint
        filename = getattr(item, "filename", "")
        mime = getattr(item, "mime_type", "")
        if filename:
            blob = f"{filename}:{mime}"
            return hashlib.sha256(blob.encode()).hexdigest()
        return None

    try:
        file_size = p.stat().st_size
        h = hashlib.sha256()
        h.update(str(file_size).encode())
        with open(p, "rb") as fh:
            prefix = fh.read(_MEDIA_PREFIX_BYTES)
            h.update(prefix)
        return h.hexdigest()
    except OSError as exc:
        logger.debug("dedup: cannot read %s for fingerprinting: %s", p, exc)
        return None
