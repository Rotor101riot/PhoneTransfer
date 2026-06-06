"""
backup_manager_ios.py

Manages local MobileSync-format backups of iOS devices using idevicebackup2.
Extractors (extract_contacts_ios, extract_sms_ios, etc.) use iOSbackup to read
files from the backup; this module ensures the backup exists before they run.

Backup directory layout (matches Apple's MobileSync format):
    config.temp_dir / "backups" / {udid} /
        Info.plist
        Manifest.plist
        Manifest.db
        Status.plist
        <sha1-named file blobs>

idevicebackup2 commands:
    -u <udid> backup --full <target_dir>            # full backup to dir
    info    <udid>                                  # show device info
    list    <backup_dir>                            # list backup contents

iOSbackup reads the standard Apple MobileSync format; the backup dir passed to
idevicebackup2 is the PARENT that will contain the UDID subdirectory.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from core.config_loader import Config, get_config

# Matches pymobiledevice3 backup2 tqdm output, e.g.:
#   1%|1         | 1.01/100 [00:36<59:45, 36.22s/it]
_TQDM_RE = re.compile(r"(\d+\.?\d*)/100[^\[]*\[[\d:]+<([\d:]+)")

# Matches idevicebackup2 progress output, e.g.:
#   Sent 14% (123456 of 987654 bytes) of backup data.
_IDEVICEBACKUP2_RE = re.compile(r"Sent\s+(\d+)%")

# Minimum free space required on the backup drive (bytes).
_MIN_FREE_BYTES = 15 * 1024 ** 3  # 15 GB

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _best_backup_root(desired: Path) -> Path:
    """
    Return *desired* if its drive has >= 15 GB free, otherwise return a
    ``PhoneTransfer/backups`` subfolder on whichever drive has the most
    free space (skips CD-ROM / unready drives silently).
    """
    import shutil
    import string

    def _free(p: Path) -> int:
        try:
            return shutil.disk_usage(p).free
        except Exception:
            return 0

    drive_root = Path(desired.drive + "\\") if desired.drive else desired.anchor
    if _free(drive_root) >= _MIN_FREE_BYTES:
        return desired  # plenty of room on the original drive

    # Scan all drive letters, pick the one with most free space.
    best_drive: Path | None = None
    best_free = 0
    for letter in string.ascii_uppercase:
        candidate = Path(f"{letter}:\\")
        free = _free(candidate)
        if free > best_free:
            best_free = free
            best_drive = candidate

    if best_drive is None or best_free < _MIN_FREE_BYTES:
        logger.warning(
            "backup: no drive has >= 15 GB free; using %s anyway", desired
        )
        return desired

    alt = best_drive / "PhoneTransfer" / "backups"
    logger.info(
        "backup: %s has only %.1f GB free — redirecting backup to %s (%.1f GB free)",
        drive_root,
        _free(drive_root) / 1024 ** 3,
        alt,
        best_free / 1024 ** 3,
    )
    return alt

def _is_valid_backup(backup_dir: Path) -> bool:
    """Check that the backup directory contains the minimum required files."""
    required = ["Manifest.plist", "Manifest.db", "Status.plist"]
    return all((backup_dir / f).exists() for f in required)


def _limd_env(cfg: Config) -> dict:
    """Return an env dict with libimobiledevice dir prepended to PATH."""
    env = os.environ.copy()
    env["PATH"] = str(cfg.libimobiledevice_dir) + os.pathsep + env.get("PATH", "")
    return env


def _read_backup_account_info(backup_dir: Path) -> dict:
    """
    Pull identifying metadata out of a MobileSync backup's ``Info.plist``.

    ``Info.plist`` lives at the top of each backup and carries the device's
    display name plus — on iOS 12+ — the Apple ID tied to the backup.  The
    Apple ID field name has drifted: recent backups use ``iCloud Account``
    or ``Apple ID``; older ones only expose ``Display Name``.  Missing
    fields return ``None`` rather than raising, so callers can log whatever
    is available.
    """
    info_path = backup_dir / "Info.plist"
    out: dict[str, str | None] = {
        "display_name": None,
        "apple_id": None,
        "last_backup_date": None,
        "product_name": None,
        "serial_number": None,
    }
    if not info_path.exists():
        return out
    try:
        import plistlib
        with open(info_path, "rb") as fh:
            info = plistlib.load(fh)
    except Exception as exc:
        logger.debug("_read_backup_account_info: parse failed: %s", exc)
        return out

    for src_key, out_key in (
        ("Display Name", "display_name"),
        ("Device Name", "display_name"),
        ("Apple ID", "apple_id"),
        ("iCloud Account", "apple_id"),
        ("Last Backup Date", "last_backup_date"),
        ("Product Name", "product_name"),
        ("Serial Number", "serial_number"),
    ):
        val = info.get(src_key)
        if val is not None and out[out_key] is None:
            out[out_key] = str(val)
    return out


# ---------------------------------------------------------------------------
# BackupManager
# ---------------------------------------------------------------------------

class BackupManager:
    """
    Orchestrates creating and managing local MobileSync backups of an iOS
    device via idevicebackup2.

    Typical usage by an extractor:
        mgr = BackupManager(udid)
        if not mgr.ensure_backup():
            return []
        # ... proceed to read mgr.backup_dir with iOSbackup ...
    """

    def __init__(self, udid: str, config: Config | None = None) -> None:
        """
        Args:
            udid:   The UDID of the target iOS device.
            config: Optional pre-loaded Config; if None, get_config() is called.
        """
        self.udid: str = udid
        self.cfg: Config = config if config is not None else get_config()

        # Determine backup_root and backup_dir.
        #
        # backup_dir_override can be either:
        #   (a) A direct path to an existing UDID backup folder
        #       (contains Manifest.plist) — used as-is, no new backup run.
        #   (b) A root directory where new backups should be written
        #       (no Manifest.plist) — pymobiledevice3 creates <root>/<udid>/.
        #
        # When not set, default to temp_dir/backups/<udid>.
        if self.cfg.backup_dir_override is not None:
            override = self.cfg.backup_dir_override
            if _is_valid_backup(override):
                # (a) Direct path to an existing backup
                self.backup_dir: Path = override
                self.backup_root: Path = override.parent
            else:
                # (b) Root directory for new backups
                self.backup_root = override
                self.backup_dir = override / udid
        else:
            # Auto-redirect to a drive with sufficient free space if C: is full.
            self.backup_root = _best_backup_root(self.cfg.temp_dir / "backups")
            self.backup_dir = self.backup_root / udid

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def ensure_backup(
        self,
        force: bool = False,
        timeout_seconds: int = 7200,
        on_progress: Callable[[float, str], None] | None = None,
    ) -> bool:
        """
        Ensure a local backup exists for the device.

        If ``cfg.backup_dir_override`` is set, the backup at that path is
        validated and used as-is — idevicebackup2 is never run.

        If a backup already exists at the default location and ``force=False``,
        returns True immediately.  If ``force=True`` or no valid backup is
        found, runs a full backup via idevicebackup2.

        Args:
            force:           When True, always re-run the backup even if one
                             already exists locally.  Ignored when
                             backup_dir_override is set.
            timeout_seconds: Maximum seconds to wait for idevicebackup2 to
                             complete before treating it as a failure.

        Returns:
            True on success (backup exists and is valid), False on failure.
        """
        if self.cfg.backup_dir_override is not None:
            if _is_valid_backup(self.backup_dir):
                # Existing valid backup found (direct path or already written to root).
                logger.info(
                    "Using existing backup for %s at %s",
                    self.udid, self.backup_dir,
                )
                return True
            if self.backup_dir == self.cfg.backup_dir_override:
                # Override was a direct backup path but no valid backup there.
                logger.error(
                    "backup_dir_override set to %s but no valid backup found "
                    "(expected Manifest.plist, Manifest.db, Status.plist).",
                    self.backup_dir,
                )
                return False
            # Override is a root dir — run a new backup into it.
            logger.info(
                "Writing new backup for %s to %s", self.udid, self.backup_root
            )
            return self._run_backup(timeout_seconds, on_progress)

        # Delete any existing backup so the next run always captures fresh device
        # state.  Without this, a stale backup from a previous session silently
        # provides old data.  pymobiledevice3 backup2 (without --full) will
        # perform an incremental update if we keep the directory, but deleting
        # first guarantees a clean slate.
        if self.backup_dir.exists():
            import shutil
            age = self.backup_age_hours()
            age_str = f" ({age:.1f}h old)" if age is not None else ""
            logger.info(
                "Removing existing backup for %s%s — running fresh.",
                self.udid, age_str,
            )
            try:
                shutil.rmtree(self.backup_dir)
            except Exception as exc:
                logger.warning("Could not remove existing backup at %s: %s", self.backup_dir, exc)

        return self._run_backup(timeout_seconds, on_progress)

    # ------------------------------------------------------------------
    # Device-side backup encryption control
    # ------------------------------------------------------------------

    def device_will_encrypt(self) -> bool:
        """
        Return True if the device currently has backup encryption enabled.

        Queries the device live via ``Mobilebackup2Service.get_will_encrypt()``.
        Returns False on any error (device not connected, pmd3 unavailable, etc.).
        """
        try:
            from core.device_connection_cache import get_lockdown
            from core.pmd3_asyncio import pmd3_run
            from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service

            lockdown = get_lockdown(self.udid)

            async def _check() -> bool:
                async with Mobilebackup2Service(lockdown) as mb2:
                    return await mb2.get_will_encrypt()

            return bool(pmd3_run(_check()))
        except Exception as exc:
            logger.debug("device_will_encrypt: %s", exc)
            return False

    def enable_backup_encryption(self, password: str) -> bool:
        """
        Enable iTunes backup encryption on the device using *password*.

        Returns True on success, False on any error.
        The device must be trusted (paired) and connected.
        """
        try:
            from core.device_connection_cache import get_lockdown
            from core.pmd3_asyncio import pmd3_run
            from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service

            lockdown = get_lockdown(self.udid)
            self.backup_root.mkdir(parents=True, exist_ok=True)

            async def _enable() -> None:
                async with Mobilebackup2Service(lockdown) as mb2:
                    await mb2.change_password(
                        backup_directory=str(self.backup_root),
                        old="",
                        new=password,
                    )

            pmd3_run(_enable())
            logger.info("backup: encryption enabled on device %s", self.udid)
            return True
        except Exception as exc:
            logger.error("backup: failed to enable encryption on %s: %s", self.udid, exc)
            return False

    def disable_backup_encryption(self, password: str) -> bool:
        """
        Disable iTunes backup encryption on the device.

        *password* must be the current backup password.
        Returns True on success, False on any error.
        """
        try:
            from core.device_connection_cache import get_lockdown
            from core.pmd3_asyncio import pmd3_run
            from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service

            lockdown = get_lockdown(self.udid)
            self.backup_root.mkdir(parents=True, exist_ok=True)

            async def _disable() -> None:
                async with Mobilebackup2Service(lockdown) as mb2:
                    await mb2.change_password(
                        backup_directory=str(self.backup_root),
                        old=password,
                        new="",
                    )

            pmd3_run(_disable())
            logger.info("backup: encryption disabled on device %s", self.udid)
            return True
        except Exception as exc:
            logger.error("backup: failed to disable encryption on %s: %s", self.udid, exc)
            return False

    def ensure_backup_for_transfer(
        self,
        force: bool = False,
        timeout_seconds: int = 7200,
        on_progress: Callable[[float, str], None] | None = None,
        on_password_needed: Callable[[], "str | None"] | None = None,
    ) -> bool:
        """
        Ensure a valid backup exists and, if it is encrypted, decrypt it.

        Reads ``cfg.backup_password`` to determine whether decryption should
        be attempted.  If the backup is encrypted and no password is set,
        ``on_password_needed`` is called (blocking the caller until the user
        responds).  Pass a thread-safe UI callback here so the password dialog
        appears *after* the backup completes rather than before it starts.

        Args:
            force:               Forwarded to :meth:`ensure_backup`.
            timeout_seconds:     Forwarded to :meth:`ensure_backup`.
            on_progress:         Optional tqdm/progress callback.
            on_password_needed:  Callable() → password string or None.
                                 Invoked on the caller's thread when an
                                 encrypted backup is detected and no password
                                 has been pre-supplied via cfg.backup_password.

        Returns:
            True if a usable backup exists (decrypted or unencrypted), False
            if no backup could be obtained or decryption failed hard.
        """
        # ------------------------------------------------------------------
        # Auto-enable backup encryption when a password is configured and
        # we are about to run a fresh backup.  We disable it afterwards in
        # a finally block so the device always reverts to its original state.
        # ------------------------------------------------------------------
        encryption_password = self.cfg.backup_password
        we_enabled_encryption = False
        needs_fresh_backup = (
            not self.backup_exists() or force
        ) and self.cfg.backup_dir_override is None

        from core.settings_manager import get_settings as _get_settings
        _s = _get_settings()
        if encryption_password and needs_fresh_backup and _s.ios_auto_enable_encryption:
            if not self.device_will_encrypt():
                logger.info(
                    "backup: enabling device encryption for %s "
                    "(will disable after backup completes)",
                    self.udid,
                )
                we_enabled_encryption = self.enable_backup_encryption(encryption_password)
                if not we_enabled_encryption:
                    logger.warning(
                        "backup: could not enable encryption on device %s — "
                        "call history and health data may be absent from backup.",
                        self.udid,
                    )
            else:
                logger.debug(
                    "backup: device %s already has encryption enabled", self.udid
                )

        try:
            if not self.ensure_backup(force=force, timeout_seconds=timeout_seconds, on_progress=on_progress):
                return False
        finally:
            if we_enabled_encryption:
                logger.info(
                    "backup: disabling device encryption for %s "
                    "(restoring original unencrypted state)",
                    self.udid,
                )
                self.disable_backup_encryption(encryption_password)

        if self.is_encrypted():
            password = self.cfg.backup_password
            if not password and on_password_needed is not None:
                logger.info(
                    "Backup for %s is encrypted — prompting for password.", self.udid
                )
                password = on_password_needed()
                if password:
                    self.cfg.backup_password = password
            if password and not _s.ios_auto_decrypt_backup:
                logger.info(
                    "Backup for %s is encrypted — skipping auto-decrypt (disabled in settings). "
                    "Registering password for iOSbackup on-the-fly decryption.",
                    self.udid,
                )
                try:
                    from core.device_connection_cache import register_backup_password
                    register_backup_password(self.udid, password)
                except Exception as exc:
                    logger.warning(
                        "On-the-fly decryption registration failed for %s: %s", self.udid, exc
                    )
                return True
            if password:
                logger.info(
                    "Backup for %s is encrypted — decrypting with provided password.",
                    self.udid,
                )
                decrypted = self.decrypt_backup(password)
                if decrypted:
                    logger.info("Backup for %s decrypted successfully.", self.udid)
                else:
                    # pymobiledevice3 BackupDecryptor unavailable — register the
                    # password so iOSbackup decrypts each file on-the-fly instead.
                    logger.info(
                        "Backup for %s: pre-decryption unavailable, "
                        "registering password for iOSbackup on-the-fly decryption.",
                        self.udid,
                    )
                    try:
                        from core.device_connection_cache import register_backup_password
                        register_backup_password(self.udid, password)
                    except Exception as exc:
                        logger.error(
                            "Backup decryption failed and on-the-fly fallback "
                            "unavailable for %s: %s", self.udid, exc
                        )
                        return False
            else:
                logger.warning(
                    "Backup for %s is encrypted but no password was provided. "
                    "Extraction will be limited to unencrypted domains only.",
                    self.udid,
                )

        # Register the confirmed backup directory so iOSbackup instances
        # opened by extractors read from our directory, not the system default.
        try:
            from core.device_connection_cache import register_backup_dir
            register_backup_dir(self.udid, self.backup_dir)
        except Exception as exc:
            logger.debug("ensure_backup_for_transfer: register_backup_dir failed: %s", exc)

        # Source integrity gate: PRAGMA integrity_check on Manifest.db.
        # If the source backup is already corrupt, extractors crash one-by-one
        # with cryptic errors — catch it here with a clear, actionable message.
        manifest_db = self.backup_dir / "Manifest.db"
        if manifest_db.exists():
            try:
                import sqlite3 as _sql
                with _sql.connect(str(manifest_db)) as _con:
                    _rows = _con.execute("PRAGMA integrity_check").fetchall()
                if not (_rows and _rows[0][0] == "ok"):
                    _lines = " | ".join(str(r[0]) for r in _rows[:5])
                    logger.error(
                        "ensure_backup_for_transfer: source Manifest.db integrity "
                        "check FAILED for %s: %s — backup is corrupt, aborting",
                        self.udid, _lines,
                    )
                    return False
                logger.debug(
                    "ensure_backup_for_transfer: source Manifest.db "
                    "integrity_check OK for %s", self.udid,
                )
            except Exception as exc:
                logger.warning(
                    "ensure_backup_for_transfer: could not verify source "
                    "Manifest.db for %s: %s — proceeding anyway",
                    self.udid, exc,
                )

        return True

    def backup_exists(self) -> bool:
        """Return True if a valid backup already exists locally."""
        return self.backup_dir.exists() and _is_valid_backup(self.backup_dir)

    def backup_age_hours(self) -> float | None:
        """
        Return the age of the most recent backup in hours.

        Reads the ``Date`` field from ``Status.plist`` inside the backup
        directory.  Returns None if no backup exists or the date cannot be
        read.
        """
        status_plist = self.backup_dir / "Status.plist"
        if not status_plist.exists():
            return None
        try:
            import plistlib
            with open(status_plist, "rb") as f:
                status = plistlib.load(f)
            backup_date = status.get("Date")
            if backup_date:
                from datetime import timezone
                now = datetime.now(timezone.utc)
                if backup_date.tzinfo is None:
                    backup_date = backup_date.replace(tzinfo=timezone.utc)
                delta = now - backup_date
                return delta.total_seconds() / 3600
        except Exception as exc:
            logger.debug("Could not read backup date: %s", exc)
        return None

    def delete_backup(self) -> bool:
        """
        Delete the local backup directory to free disk space.

        Returns:
            True if the directory was deleted (or did not exist), False on error.
        """
        import shutil

        if not self.backup_dir.exists():
            return True
        try:
            shutil.rmtree(self.backup_dir)
            logger.info("Deleted local backup for %s", self.udid)
            return True
        except Exception as exc:
            logger.error("Failed to delete backup: %s", exc)
            return False

    def is_encrypted(self) -> bool:
        """
        Return True if the local backup is encrypted.

        Reads the ``IsEncrypted`` key from ``Manifest.plist`` inside the
        backup directory.  Returns False if the plist is absent or unreadable.
        """
        manifest = self.backup_dir / "Manifest.plist"
        if not manifest.exists():
            return False
        try:
            import plistlib
            with open(manifest, "rb") as f:
                data = plistlib.load(f)
            return bool(data.get("IsEncrypted", False))
        except Exception as exc:
            logger.debug("is_encrypted: could not read Manifest.plist: %s", exc)
            return False

    def decrypt_backup(self, password: str) -> bool:
        """
        Decrypt an encrypted MobileSync backup in-place using the provided
        password.

        Tries the pymobiledevice3 backup decryption API in the order that
        known versions expose it.  Returns True on success, False if
        decryption failed or no decryption support is available.

        Args:
            password: The iTunes backup password set on the device.

        Returns:
            True on success, False on failure.
        """
        if not self.backup_dir.exists():
            logger.error(
                "decrypt_backup: no backup found at %s", self.backup_dir
            )
            return False

        # --- Attempt 1: pymobiledevice3 >= 3.x (backup_decryptor module) ---
        try:
            from pymobiledevice3.backup.backup_decryptor import (  # type: ignore[import]
                BackupDecryptor,
            )
            d = BackupDecryptor(str(self.backup_dir), password)
            d.decrypt()
            logger.info(
                "decrypt_backup: backup for %s decrypted (API: backup_decryptor)",
                self.udid,
            )
            return True
        except ImportError:
            pass
        except Exception as exc:
            logger.error("decrypt_backup: BackupDecryptor failed: %s", exc)
            return False

        # --- Attempt 2: older import path ------------------------------------
        try:
            from pymobiledevice3.backup import BackupDecryptor  # type: ignore[import]
            d = BackupDecryptor(str(self.backup_dir), password)
            d.decrypt()
            logger.info(
                "decrypt_backup: backup for %s decrypted (API: backup module)",
                self.udid,
            )
            return True
        except ImportError:
            pass
        except Exception as exc:
            logger.error("decrypt_backup: BackupDecryptor (alt path) failed: %s", exc)
            return False

        logger.warning(
            "decrypt_backup: no backup decryption support found in pymobiledevice3. "
            "Ensure pymobiledevice3 >= 3.x is installed."
        )
        return False

    def delete_backup_if_safe(self) -> bool:
        """
        Verify the backup directory and then delete it to reclaim disk space.

        Only runs when the backup was created by PhoneTransfer (i.e. no
        ``backup_dir_override`` pointing at a user-supplied backup).  Performs
        a SQLite integrity check on ``Manifest.db`` first; if the check fails
        the backup is kept and False is returned.

        Returns:
            True  — backup verified and deleted.
            False — skipped (override path) or verification failed; backup retained.
        """
        # Never touch a backup the user pointed us at manually.
        if self.cfg.backup_dir_override is not None:
            logger.info(
                "delete_backup_if_safe: skipping — backup_dir_override is set (%s)",
                self.cfg.backup_dir_override,
            )
            return False

        if not self.backup_dir.exists():
            logger.debug("delete_backup_if_safe: backup_dir does not exist, nothing to delete")
            return False

        # Verify Manifest.db integrity before deleting.
        manifest_db = self.backup_dir / "Manifest.db"
        if not manifest_db.exists():
            logger.warning(
                "delete_backup_if_safe: Manifest.db missing at %s — keeping backup",
                self.backup_dir,
            )
            return False

        try:
            import sqlite3
            with sqlite3.connect(str(manifest_db)) as con:
                row = con.execute("PRAGMA integrity_check").fetchone()
                if row is None or row[0].lower() != "ok":
                    logger.warning(
                        "delete_backup_if_safe: Manifest.db integrity_check = %s "
                        "— keeping backup for %s",
                        row,
                        self.udid,
                    )
                    return False
        except Exception as exc:
            logger.warning(
                "delete_backup_if_safe: could not verify Manifest.db for %s: %s "
                "— keeping backup",
                self.udid,
                exc,
            )
            return False

        import shutil
        try:
            shutil.rmtree(self.backup_dir)
            logger.info(
                "delete_backup_if_safe: deleted backup for %s at %s (space reclaimed)",
                self.udid,
                self.backup_dir,
            )
            return True
        except Exception as exc:
            logger.warning(
                "delete_backup_if_safe: could not delete %s: %s",
                self.backup_dir,
                exc,
            )
            return False

    def list_backup_contents(self) -> list[dict]:
        """
        Return a list of file entries from the backup ``Manifest.db``.

        Each entry is a dict with keys:
            ``domain``       — iTunes backup domain (e.g. ``AppDomain-…``)
            ``relativePath`` — path of the file within that domain
            ``fileSize``     — size in bytes (0 when NULL in the DB)

        Returns an empty list if the manifest does not exist or cannot be read.
        Useful for debugging or estimating extraction scope.
        """
        manifest_db = self.backup_dir / "Manifest.db"
        if not manifest_db.exists():
            return []
        import sqlite3
        try:
            with sqlite3.connect(str(manifest_db)) as con:
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    "SELECT domain, relativePath, fileSize FROM Files ORDER BY domain, relativePath"
                ).fetchall()
            return [
                {
                    "domain": r["domain"],
                    "relativePath": r["relativePath"],
                    "fileSize": r["fileSize"] or 0,
                }
                for r in rows
            ]
        except Exception as exc:
            logger.debug("Manifest.db read failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def restore_backup(
        self,
        backup_root: Path,
        password: str | None = None,
        *,
        live: bool = False,
        system: bool = True,
        reboot: bool = True,
        copy: bool = True,
        settings: bool = True,
        remove: bool = False,
        timeout_seconds: int = 14400,
        on_progress: Callable[[float, str], None] | None = None,
    ) -> bool:
        """
        Restore a (modified) backup to this device via ``pymobiledevice3 backup2 restore``.

        ``backup_root`` is the parent directory that contains the ``<udid>/``
        subfolder (the same layout pymobiledevice3 produces on backup).  This
        is what :meth:`IOSBackupRepacker.commit` writes its output to.

        Defaults with ``remove=False`` make the restore *additive*: files on
        the device that aren't in the backup are kept in place, and rows
        already present in the device's DBs are unchanged.  When the backup
        being restored is the device's OWN just-captured backup plus a few
        injected rows, the net effect is a merge — nothing the user had
        goes away.

        ``live`` tells the method whether this call is part of a still-
        running transfer pipeline (backup → inject → restore, end-to-end,
        same device, same session):

          * ``live=True``  — soft framing in logs; matches how Dr.Fone and
            similar tools present the step.  Safe when the backup was
            captured seconds ago and no iCloud/account change has occurred.
          * ``live=False`` — hard warning.  A deferred restore against a
            stale repacked backup can clobber real changes the user has
            made since the capture, and if the device's iCloud account has
            changed in the interim the restore can strand Keychain items,
            iMessage registration, and purchased-app entitlements.

        ``password`` is required when the backup is encrypted; pymobiledevice3
        prompts otherwise, which would hang in headless mode.
        """
        import sys

        if not (backup_root / self.udid / "Manifest.plist").exists():
            logger.error(
                "backup: restore source not found at %s/%s/Manifest.plist",
                backup_root, self.udid,
            )
            return False

        account = _read_backup_account_info(backup_root / self.udid)
        if live:
            logger.info(
                "backup: merging modified backup into %s (same-session, "
                "additive restore — nothing currently on the device is "
                "deleted).  Source: %s", self.udid, backup_root,
            )
            if account.get("display_name") or account.get("apple_id"):
                logger.info(
                    "backup: restore source is a backup of %s (Apple ID: %s).  "
                    "If the device has signed into a different iCloud account "
                    "since this backup was captured, abort now.",
                    account.get("display_name") or "<unknown device>",
                    account.get("apple_id") or "<not recorded in backup>",
                )
        else:
            logger.warning(
                "backup: DEFERRED RESTORE — this backup was captured earlier "
                "and may not reflect the device's current state.  Proceeding "
                "will overwrite any app/user data that changed since then, "
                "and if the device's iCloud account has changed since the "
                "capture it will strand Keychain items, iMessage registration, "
                "and purchased-app entitlements.  Source: %s", backup_root,
            )
            logger.warning(
                "backup: confirm the target device is still the same one "
                "signed into the same Apple ID that produced this backup.  "
                "Source device: %s  Apple ID in backup: %s",
                account.get("display_name") or "<unknown>",
                account.get("apple_id") or "<not recorded in backup>",
            )

        cmd = [
            sys.executable, "-m", "pymobiledevice3",
            "backup2", "restore",
            "--udid", self.udid,
        ]
        if system:
            cmd.append("--system")
        if reboot:
            cmd.append("--reboot")
        if copy:
            cmd.append("--copy")
        if settings:
            cmd.append("--settings")
        if remove:
            cmd.append("--remove")
        if password:
            cmd.extend(["--password", password])
        cmd.append(str(backup_root))

        logger.info(
            "backup: invoking pymobiledevice3 restore: %s",
            " ".join(
                "***" if i > 0 and cmd[i - 1] == "--password" else c
                for i, c in enumerate(cmd)
            ),
        )

        return self._run_subprocess_backup(
            cmd=cmd,
            env=None,
            progress_re=_TQDM_RE,
            progress_group=(1, 2),
            timeout_seconds=timeout_seconds,
            on_progress=on_progress,
            label="pymobiledevice3 restore",
        )

    def _run_backup(
        self,
        timeout_seconds: int,
        on_progress: Callable[[float, str], None] | None = None,
    ) -> bool:
        """
        Dispatch to the configured backup backend.

        When ``ios_backup_driver`` is ``"libimobiledevice"``, uses
        idevicebackup2 directly.  Otherwise uses pymobiledevice3 backup2 and
        automatically falls back to idevicebackup2 on failure (covers iOS 7-8
        where pmd3 backup2 is unreliable).
        """
        from core.settings_manager import get_settings as _get_settings
        driver = _get_settings().ios_backup_driver

        if driver == "libimobiledevice":
            return self._run_idevicebackup2(timeout_seconds, on_progress)

        # Default: pymobiledevice3 with automatic idevicebackup2 fallback.
        ok = self._run_pmd3_backup(timeout_seconds, on_progress)
        if not ok and _is_valid_backup(self.backup_dir):
            # pmd3 returned non-zero but left a usable partial backup.
            return True
        if not ok:
            logger.info(
                "backup: pymobiledevice3 failed for %s — "
                "retrying with idevicebackup2 (iOS 7-8 fallback)",
                self.udid,
            )
            return self._run_idevicebackup2(timeout_seconds, on_progress)
        return True

    def _run_pmd3_backup(
        self,
        timeout_seconds: int,
        on_progress: Callable[[float, str], None] | None = None,
    ) -> bool:
        """
        Run ``pymobiledevice3 backup2 backup`` for this device.

        Args:
            timeout_seconds: Hard upper bound; process is killed if exceeded.
            on_progress:     Optional callback called as ``(pct, eta_str)``
                             whenever a new tqdm line is parsed from stderr.

        Returns:
            True if the backup exits with return code 0, False otherwise.
        """
        import sys

        self.backup_root.mkdir(parents=True, exist_ok=True)

        from core.settings_manager import get_settings as _get_settings
        _force_full = _get_settings().ios_force_full_backup
        cmd = [
            sys.executable, "-m", "pymobiledevice3",
            "backup2", "backup",
            "--udid", self.udid,
        ]
        if _force_full:
            cmd.append("--full")
        cmd.append(str(self.backup_root))
        logger.info("Starting iOS backup (pymobiledevice3): %s", " ".join(cmd))

        return self._run_subprocess_backup(
            cmd=cmd,
            env=None,
            progress_re=_TQDM_RE,
            progress_group=(1, 2),
            timeout_seconds=timeout_seconds,
            on_progress=on_progress,
            label="pymobiledevice3",
        )

    def _run_idevicebackup2(
        self,
        timeout_seconds: int,
        on_progress: Callable[[float, str], None] | None = None,
    ) -> bool:
        """
        Run ``idevicebackup2 backup`` for this device.

        Uses the libimobiledevice binary from ``cfg.idevice_bins``.  The DLL
        directory is prepended to PATH so the binary can find its dependencies.

        Args:
            timeout_seconds: Hard upper bound; process is killed if exceeded.
            on_progress:     Optional callback.

        Returns:
            True if the backup exits with return code 0, False otherwise.
        """
        idb2 = self.cfg.idevice_bins.get("idevicebackup2")
        if idb2 is None:
            logger.error(
                "backup: idevicebackup2 not found in cfg.idevice_bins — "
                "ensure bin/libimobiledevice/idevicebackup2.exe is present"
            )
            return False

        self.backup_root.mkdir(parents=True, exist_ok=True)

        from core.settings_manager import get_settings as _get_settings
        _force_full = _get_settings().ios_force_full_backup
        cmd = [str(idb2), "-u", self.udid, "backup"]
        if _force_full:
            cmd.append("--full")
        cmd.append(str(self.backup_root))
        logger.info("Starting iOS backup (idevicebackup2): %s", " ".join(cmd))

        return self._run_subprocess_backup(
            cmd=cmd,
            env=_limd_env(self.cfg),
            progress_re=_IDEVICEBACKUP2_RE,
            progress_group=(1, None),   # group 1 = pct, no ETA in output
            timeout_seconds=timeout_seconds,
            on_progress=on_progress,
            label="idevicebackup2",
        )

    def _run_subprocess_backup(
        self,
        cmd: list[str],
        env: dict | None,
        progress_re: "re.Pattern[str]",
        progress_group: tuple[int, int | None],
        timeout_seconds: int,
        on_progress: Callable[[float, str], None] | None,
        label: str,
    ) -> bool:
        """
        Shared subprocess runner for both backup backends.

        Streams stderr, parses progress via *progress_re*, polls
        ``cfg.cancel_event`` every 0.5 s, and enforces *timeout_seconds*.
        """
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        except FileNotFoundError:
            logger.error("backup: %s binary not found", label)
            return False

        _stderr_tail: list[str] = []
        pct_group, eta_group = progress_group

        def _read_stderr() -> None:
            buf = ""
            assert proc.stderr is not None
            for chunk in iter(lambda: proc.stderr.read(256), ""):
                buf += chunk
                parts = re.split(r"[\r\n]", buf)
                buf = parts[-1]
                for line in parts[:-1]:
                    stripped = line.strip()
                    if stripped:
                        _stderr_tail.append(stripped)
                        if len(_stderr_tail) > 20:
                            _stderr_tail.pop(0)
                    if on_progress:
                        m = progress_re.search(line)
                        if m:
                            try:
                                pct = float(m.group(pct_group))
                                eta = m.group(eta_group) if eta_group else ""
                                on_progress(pct, eta)
                            except Exception:
                                pass

        reader = threading.Thread(target=_read_stderr, daemon=True)
        reader.start()

        cancel_ev = self.cfg.cancel_event
        deadline = time.monotonic() + timeout_seconds

        while True:
            try:
                proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if cancel_ev is not None and cancel_ev.is_set():
                    proc.kill()
                    proc.communicate()
                    reader.join(timeout=2)
                    logger.info("iOS backup cancelled for %s", self.udid)
                    return False
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.communicate()
                    reader.join(timeout=2)
                    logger.error("iOS backup timed out after %ds", timeout_seconds)
                    return False

        reader.join(timeout=2)
        if proc.returncode == 0:
            logger.info("iOS backup complete (%s) for %s", label, self.udid)
            return True
        logger.error(
            "iOS backup failed (%s, rc=%d): %s",
            label,
            proc.returncode,
            "\n".join(_stderr_tail),
        )
        return False


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

def ensure_backup_for(
    udid: str,
    force: bool = False,
    timeout_seconds: int = 7200,
) -> bool:
    """
    Convenience wrapper: create a :class:`BackupManager` and call
    :meth:`~BackupManager.ensure_backup`.

    Intended for use by extractors that need to guarantee a backup exists
    before they begin reading data.

    Example::

        from core.backup_manager_ios import ensure_backup_for

        def extract_contacts(udid: str) -> list[dict]:
            if not ensure_backup_for(udid):
                return []
            # ... read from backup with iOSbackup ...

    Args:
        udid:            UDID of the target iOS device.
        force:           When True, always re-run the full backup.
        timeout_seconds: Maximum seconds to wait for idevicebackup2.

    Returns:
        True on success, False on failure.
    """
    mgr = BackupManager(udid)
    return mgr.ensure_backup(force=force, timeout_seconds=timeout_seconds)


def get_backup_dir(udid: str) -> Path:
    """
    Return the expected backup directory path for a given UDID.

    This is a pure path computation — it does not check whether the backup
    actually exists.

    Args:
        udid: UDID of the iOS device.

    Returns:
        ``config.temp_dir / "backups" / udid``
    """
    cfg = get_config()
    return cfg.temp_dir / "backups" / udid
