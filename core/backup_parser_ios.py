"""
backup_parser_ios.py

Utility for reading files out of a local MobileSync-format iOS backup.

The Manifest.db (SQLite) at the root of the backup directory maps each backed-up
file to a SHA-1 hash (fileID).  The actual blob lives at:

    backup_dir / fileID[:2] / fileID      (no extension)

Common domain strings and the paths that matter for PhoneTransfer:

    "AppDomain-com.apple.MobileSMS"
        Library/SMS/sms.db

    "AppDomain-com.apple.mobilephone"
        Library/CallHistoryDB/CallHistory.storedata  (or call_history.db)

    "AppDomain-com.apple.MobileAddressBook"
        Library/AddressBook/AddressBook.sqlitedb
        Library/AddressBook/AddressBookImages.sqlitedb

    "HomeDomain"
        Library/Calendar/Calendar.sqlitedb

    "AppDomain-com.apple.reminders"
        Library/Reminders/Container_v1/Stores/<uuid>.sqlite
        (or exported JSON depending on iOS version)

Usage
-----
    from core.backup_parser_ios import open_backup

    with open_backup(backup_dir) as bp:
        blob = bp.open_file(
            "AppDomain-com.apple.MobileSMS",
            "Library/SMS/sms.db",
        )
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_MANIFEST_NAME = "Manifest.db"


class BackupParser:
    """
    Read-only access to an iOS MobileSync backup directory.

    The Manifest.db (SQLite) at the backup root stores a ``Files`` table
    with columns including ``fileID`` (SHA-1 hex), ``domain``, and
    ``relativePath``.  Blob files are stored without an extension at::

        backup_dir / fileID[:2] / fileID

    Parameters
    ----------
    backup_dir:
        Path to the root of the extracted iOS backup (the directory that
        contains Manifest.db, Info.plist, etc.).
    """

    def __init__(self, backup_dir: Path) -> None:
        self.backup_dir = Path(backup_dir)
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """Lazily open and cache the Manifest.db connection."""
        if self._conn is None:
            manifest_path = self.backup_dir / _MANIFEST_NAME
            if not manifest_path.exists():
                raise FileNotFoundError(
                    f"Manifest.db not found in backup directory: {self.backup_dir}"
                )
            self._conn = sqlite3.connect(str(manifest_path))
            self._conn.row_factory = sqlite3.Row
            logger.debug("Opened Manifest.db: %s", manifest_path)
        return self._conn

    def close(self) -> None:
        """Close the Manifest.db SQLite connection."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            finally:
                self._conn = None

    def __enter__(self) -> "BackupParser":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _blob_path(self, file_id: str) -> Path:
        """Return the filesystem path for a given fileID."""
        return self.backup_dir / file_id[:2] / file_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_file(self, domain: str, relative_path: str) -> Path | None:
        """
        Locate a single file by domain and relative path.

        Queries Manifest.db for the SHA-1 fileID, constructs the blob
        path and verifies it exists on disk.

        Parameters
        ----------
        domain:
            iOS backup domain string, e.g.
            ``"AppDomain-com.apple.MobileSMS"``.
        relative_path:
            Relative path within the domain, e.g.
            ``"Library/SMS/sms.db"``.

        Returns
        -------
        Path to the blob file, or ``None`` if not found.
        """
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "SELECT fileID FROM Files WHERE domain = ? AND relativePath = ?",
                (domain, relative_path),
            )
            row = cur.fetchone()
        except sqlite3.Error as exc:
            logger.error("Manifest.db query failed: %s", exc)
            return None

        if row is None:
            logger.debug("find_file: not found — domain=%s path=%s", domain, relative_path)
            return None

        file_id: str = row["fileID"]
        blob = self._blob_path(file_id)
        if not blob.exists():
            logger.warning(
                "fileID %s found in Manifest.db but blob missing on disk: %s",
                file_id, blob,
            )
            return None

        logger.debug("find_file: found %s -> %s", relative_path, blob)
        return blob

    def find_files_in_domain(
        self,
        domain: str,
        path_prefix: str = "",
    ) -> list[dict]:
        """
        Return all files for *domain*, optionally filtered by *path_prefix*.

        Parameters
        ----------
        domain:
            iOS backup domain string.
        path_prefix:
            If non-empty, only entries whose ``relativePath`` starts with
            this string are returned.

        Returns
        -------
        List of dicts with keys:
            ``domain``, ``relativePath``, ``fileID``, ``blob_path`` (Path).
        """
        conn = self._get_conn()
        try:
            if path_prefix:
                cur = conn.execute(
                    "SELECT fileID, domain, relativePath FROM Files "
                    "WHERE domain = ? AND relativePath LIKE ?",
                    (domain, path_prefix + "%"),
                )
            else:
                cur = conn.execute(
                    "SELECT fileID, domain, relativePath FROM Files WHERE domain = ?",
                    (domain,),
                )
            rows = cur.fetchall()
        except sqlite3.Error as exc:
            logger.error("Manifest.db query failed: %s", exc)
            return []

        results: list[dict] = []
        for row in rows:
            file_id = row["fileID"]
            blob = self._blob_path(file_id)
            results.append(
                {
                    "domain": row["domain"],
                    "relativePath": row["relativePath"],
                    "fileID": file_id,
                    "blob_path": blob,
                }
            )
        logger.debug(
            "find_files_in_domain: domain=%s prefix=%r → %d files",
            domain, path_prefix, len(results),
        )
        return results

    def open_file(self, domain: str, relative_path: str) -> bytes | None:
        """
        Read and return the raw bytes of a backed-up file.

        Parameters
        ----------
        domain:
            iOS backup domain string.
        relative_path:
            Relative path within the domain.

        Returns
        -------
        Raw bytes of the file, or ``None`` if the file is not found.
        """
        blob = self.find_file(domain, relative_path)
        if blob is None:
            return None
        try:
            return blob.read_bytes()
        except OSError as exc:
            logger.error("open_file: failed to read blob %s: %s", blob, exc)
            return None

    def extract_db(
        self,
        domain: str,
        relative_path: str,
        dest: Path,
    ) -> Path | None:
        """
        Copy the blob for *relative_path* in *domain* to *dest*.

        Creates parent directories for *dest* as needed.

        Parameters
        ----------
        domain:
            iOS backup domain string.
        relative_path:
            Relative path within the domain.
        dest:
            Destination path on the local filesystem.

        Returns
        -------
        *dest* on success, or ``None`` on failure.
        """
        blob = self.find_file(domain, relative_path)
        if blob is None:
            return None
        try:
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(blob), str(dest))
            logger.debug("extract_db: %s -> %s", blob, dest)
            return dest
        except OSError as exc:
            logger.error("extract_db: failed to copy %s -> %s: %s", blob, dest, exc)
            return None


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

def open_backup(backup_dir: Path) -> BackupParser:
    """
    Create and return a :class:`BackupParser` for *backup_dir*.

    Intended for use as a context manager::

        with open_backup(backup_dir) as bp:
            data = bp.open_file("HomeDomain", "Library/Calendar/Calendar.sqlitedb")
    """
    return BackupParser(backup_dir)
