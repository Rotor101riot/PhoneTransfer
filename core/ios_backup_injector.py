"""
ios_backup_injector.py

Orchestrator that lets the per-category ``inject_{category}_ios`` modules
share one encrypted-backup re-pack session.

Why a separate layer on top of IOSBackupRepacker:

1. Injector modules need to READ the live DB (extract from the encrypted
   source backup, mutate in a scratch dir, write back) — not just stage
   bytes.  :meth:`IOSBackupInjector.stage_db` handles the extract-edit-
   register dance.
2. Each inject_*_ios.py must keep its existing pipeline signature
   ``inject(dev_id, items, staging, privileged) -> int``.  Passing the
   injector as an argument would break that contract, so we publish the
   active injector via a module-level context so callees can
   ``get_current_injector()`` when there is one and fall back to their
   AFC/file-push strategy when there isn't.
3. The pipeline commits *once* after every category has finished
   injecting, so a single ``repack`` pass handles SMS + Calls +
   Contacts + ... in one Manifest.db rewrite.

Usage from the pipeline manager (ios destination path)::

    with IOSBackupInjector.open(
        udid=dest.udid,
        source_backup_dir=backup_dir,
        passphrase=pw,
        staging_root=cfg.temp_dir / "ios_inject",
    ) as injector:
        # ... run all inject_*_ios.inject() calls; each picks up the
        # injector via get_current_injector() ...
        stats = injector.commit(output_dir=cfg.temp_dir / "ios_repacked")

    # pipeline_manager then hands output_dir to pymobiledevice3 to restore.

Usage from an individual injector::

    from core.ios_backup_injector import get_current_injector

    def inject(dev_id, items, staging, privileged):
        injector = get_current_injector()
        if injector is None:
            # Fall back to AFC .ics push or similar.
            return _legacy_afc_inject(dev_id, items, staging)
        db_path = injector.stage_db("HomeDomain", "Library/Calendar/Calendar.sqlitedb")
        # ... sqlite3.connect(db_path), INSERT, commit ...
        # stage_db already registers the override on commit; no extra call needed.
        return len(items)
"""

from __future__ import annotations

import contextlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from core.ios_backup_repacker import IOSBackupRepacker, RepackStats

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thread-local active injector
# ---------------------------------------------------------------------------
# A thread-local holds the currently-active injector so nested or parallel
# pipeline runs don't leak an injector handle across threads.  For the
# normal synchronous pipeline this is effectively a module global.

_tls = threading.local()


def get_current_injector() -> "IOSBackupInjector | None":
    """Return the active injector, or None when no backup session is open."""
    return getattr(_tls, "injector", None)


def _set_current(injector: "IOSBackupInjector | None") -> None:
    _tls.injector = injector


# ---------------------------------------------------------------------------
# Injector
# ---------------------------------------------------------------------------

@dataclass
class _StagedDB:
    domain: str
    relative_path: str
    local_path: Path


class IOSBackupInjector:
    """Shared backup-modification context for all iOS category injectors."""

    def __init__(
        self,
        *,
        udid: str,
        source_backup_dir: str | Path,
        passphrase: str,
        staging_root: str | Path,
    ) -> None:
        self.udid = udid
        self.source_backup_dir = Path(source_backup_dir)
        self.passphrase = passphrase
        self.staging_root = Path(staging_root)
        self.staging_root.mkdir(parents=True, exist_ok=True)

        self.repacker = IOSBackupRepacker(
            source_dir=self.source_backup_dir,
            passphrase=self.passphrase,
            scratch_dir=self.staging_root / "_scratch",
        )

        self._staged_dbs: dict[tuple[str, str], _StagedDB] = {}
        self._opened = False
        self._committed = False

    # -- context management ---------------------------------------------

    @classmethod
    @contextlib.contextmanager
    def open(
        cls,
        *,
        udid: str,
        source_backup_dir: str | Path,
        passphrase: str,
        staging_root: str | Path,
    ) -> "Iterator[IOSBackupInjector]":
        """Open an injector session and publish it as the thread-current one."""
        inst = cls(
            udid=udid,
            source_backup_dir=source_backup_dir,
            passphrase=passphrase,
            staging_root=staging_root,
        )
        inst._open()
        previous = get_current_injector()
        _set_current(inst)
        try:
            yield inst
        finally:
            _set_current(previous)
            inst._close()

    def _open(self) -> None:
        if self._opened:
            return
        self.repacker.unlock()
        self._opened = True
        logger.info(
            "IOSBackupInjector: opened session for %s  source=%s  staging=%s",
            self.udid, self.source_backup_dir, self.staging_root,
        )

    def _close(self) -> None:
        if not self._opened:
            return
        try:
            self.repacker.close()
        finally:
            self._opened = False

    # -- DB staging (extract -> mutate-in-place -> auto-register) -------

    def stage_db(self, domain: str, relative_path: str) -> Path:
        """
        Extract a live DB from the encrypted source backup into the staging
        tree and return its local path so the caller can mutate it directly
        with sqlite3 / shutil / whatever.

        The resulting path is remembered so :meth:`commit` can register it
        as an override automatically.  Calling this twice for the same
        ``(domain, relative_path)`` pair returns the previously-extracted
        path without re-extracting.
        """
        self._ensure_open()
        key = (domain, relative_path)
        existing = self._staged_dbs.get(key)
        if existing is not None:
            return existing.local_path

        local = self.staging_root / domain / relative_path
        local.parent.mkdir(parents=True, exist_ok=True)
        self.repacker.extract_file_to(domain, relative_path, local)

        # Copy any sidecar WAL/SHM files so SQLite sees a consistent snapshot.
        for suffix in ("-wal", "-shm"):
            try:
                sidecar_bytes = self.repacker.extract_file(
                    domain, relative_path + suffix
                )
            except Exception:
                continue  # sidecar absent — that's fine
            (local.parent / (local.name + suffix)).write_bytes(sidecar_bytes)

        self._staged_dbs[key] = _StagedDB(domain, relative_path, local)
        logger.debug(
            "IOSBackupInjector.stage_db: %s//%s -> %s (%d bytes)",
            domain, relative_path, local, local.stat().st_size,
        )
        return local

    def extract_file(self, domain: str, relative_path: str) -> bytes:
        """Convenience pass-through for injectors that just need to peek."""
        self._ensure_open()
        return self.repacker.extract_file(domain, relative_path)

    def list_relative_paths(
        self, domain: str, like_pattern: str
    ) -> list[str]:
        """Enumerate relativePaths in the source backup's Manifest.db.

        Used by injectors that need to discover device-specific file UUIDs
        (e.g. Reminders' per-account ``Container_v1/Stores/Data-<UUID>.sqlite``
        files).  Reads through the iphone_backup_decrypt library's already-
        open manifest connection — must be called while the injector
        session is open and before commit() closes it.
        """
        self._ensure_open()
        conn = getattr(self.repacker._backup, "_temp_manifest_db_conn", None)
        if conn is None:
            raise RuntimeError(
                "Manifest.db connection is unavailable; injector may have "
                "already committed."
            )
        rows = conn.execute(
            "SELECT relativePath FROM Files "
            "WHERE domain = ? AND relativePath LIKE ? "
            "ORDER BY relativePath",
            (domain, like_pattern),
        ).fetchall()
        return [r[0] for r in rows]

    # -- direct staging pass-throughs -----------------------------------

    def stage_override(
        self, domain: str, relative_path: str, data
    ) -> None:
        self._ensure_open()
        self.repacker.stage_override(domain, relative_path, data)

    def stage_addition(
        self,
        domain: str,
        relative_path: str,
        data,
        *,
        protection_class: int | None = None,
    ) -> None:
        self._ensure_open()
        self.repacker.stage_addition(
            domain, relative_path, data, protection_class=protection_class
        )

    def stage_deletion(self, domain: str, relative_path: str) -> None:
        self._ensure_open()
        self.repacker.stage_deletion(domain, relative_path)

    # -- commit ---------------------------------------------------------

    def commit(self, output_dir: str | Path) -> RepackStats:
        """Register every :meth:`stage_db` result, then re-pack the backup."""
        self._ensure_open()
        if self._committed:
            raise RuntimeError("IOSBackupInjector.commit() already called")

        # Auto-register any stage_db() results that the injector didn't
        # explicitly re-stage.  If the caller DID call stage_override()
        # already, that entry takes precedence (dict-keyed by path).
        for (domain, rel_path), sdb in self._staged_dbs.items():
            if (domain, rel_path) in self.repacker._overrides:
                continue
            self.repacker.stage_override(domain, rel_path, sdb.local_path)

        stats = self.repacker.commit(output_dir)
        self._committed = True
        logger.info(
            "IOSBackupInjector.commit: overrides=%d additions=%d (%.1f MB) "
            "deletions=%d dirs=%d  took %.1fs  output=%s",
            stats.overrides, stats.additions,
            stats.additions_bytes / 1024 / 1024,
            stats.deletions, stats.directories_added,
            stats.duration_seconds, stats.output_dir,
        )
        return stats

    # -- internals ------------------------------------------------------

    def _ensure_open(self) -> None:
        if not self._opened:
            raise RuntimeError(
                "IOSBackupInjector is not open; use the `with open(...)` "
                "context manager"
            )
