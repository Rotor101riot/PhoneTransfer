"""
extract_signal_android.py

Attempts to extract Signal messages from an Android device.

Current limitations (by design — these are architectural constraints,
not bugs):

1. Signal's SQLite database (signal.db) is encrypted with SQLCipher.
   The encryption key is derived from secrets held by the Android Keystore
   Hardware Abstraction Layer and is NOT accessible even with root.
   Pulling the raw database file produces a ciphertext we cannot open.

2. Signal's user-initiated backup files (.backup) are encrypted with a
   30-digit passphrase that only the device owner knows.  Without this
   passphrase automatic decryption is impossible.

What this module does instead:
- Detects whether Signal is installed on the device.
- Locates any Signal backup files on shared storage.
- Pulls the raw (encrypted) database to staging if rooted (useful for
  future decryption tooling or forensic hand-off).
- Logs clear, actionable guidance for the user at every step.
- Returns [] — an honest result that does not mislead the caller.

The structure is in place for when decryption support is added (e.g. if
Signal ever exposes a plaintext export API, or if sqlcipher3 gains
Android-Keystore key-derivation support).

Returns a list of Message objects as defined in normalization_schema.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.adb_manager import ADBManager
from core.config_loader import get_config
from core.normalization_schema import Message

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# sqlcipher3 — optional; imported only for future use
# ---------------------------------------------------------------------------

try:
    import sqlcipher3  # type: ignore[import]
    _SQLCIPHER_AVAILABLE = True
except ImportError:
    sqlcipher3 = None  # type: ignore[assignment]
    _SQLCIPHER_AVAILABLE = False

# ---------------------------------------------------------------------------
# Remote paths
# ---------------------------------------------------------------------------

_SIGNAL_PACKAGE = "org.thoughtcrime.securesms"

_DB_REMOTE_SRC = f"/data/data/{_SIGNAL_PACKAGE}/databases/signal.db"
_DB_REMOTE_TMP = "/sdcard/signal_tmp.db"

# Signal writes backups to one of these locations depending on Android version
_BACKUP_SEARCH_PATHS = [
    "/sdcard/Signal/Backups",
    f"/sdcard/Android/media/{_SIGNAL_PACKAGE}",
]

# Staging sub-directory
_SUBDIR = "signal_android"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(
    serial: str,
    staging_dir: Path,
    is_rooted: bool = False,
) -> list[Message]:
    """
    Attempts to extract Signal messages from Android.

    Current limitations:
    - Signal's SQLCipher key is protected by Android Keystore (inaccessible
      even with root on modern Android).
    - Signal backup files require the user's 30-digit passphrase.

    This extractor detects Signal installation and available backups, logs
    their location, and returns [] with appropriate user guidance.  The
    structure is in place for when decryption support is added.

    Parameters
    ----------
    serial:      ADB device serial string.
    staging_dir: Root staging directory for this transfer session.
    is_rooted:   If True, attempt to stage the encrypted DB for future use.

    Returns
    -------
    Empty list — Signal extraction is not currently automated.
    """
    try:
        return _extract_impl(serial, staging_dir, is_rooted)
    except Exception:
        logger.exception("[signal/android] Unhandled error during detection")
        return []


def _extract_impl(
    serial: str,
    staging_dir: Path,
    is_rooted: bool,
) -> list[Message]:
    sub = staging_dir / _SUBDIR
    sub.mkdir(parents=True, exist_ok=True)

    adb = ADBManager(get_config())

    # ------------------------------------------------------------------
    # Step 1: Detect whether Signal is installed
    # ------------------------------------------------------------------
    installed = _detect_installation(serial, adb)
    if not installed:
        logger.info(
            "[signal/android] Signal (%s) does not appear to be installed "
            "on device %s. Skipping.",
            _SIGNAL_PACKAGE,
            serial,
        )
        return []

    logger.info(
        "[signal/android] Signal detected on device %s.", serial
    )

    # ------------------------------------------------------------------
    # Step 2: Search for Signal backup files on shared storage
    # ------------------------------------------------------------------
    backup_paths = _find_backup_files(serial, adb)
    if backup_paths:
        logger.warning(
            "[signal/android] Signal backup file(s) found:\n%s\n"
            "Signal backups are encrypted with a 30-digit passphrase "
            "that only the device owner knows. "
            "Automatic extraction is not supported.\n"
            "To transfer Signal messages manually:\n"
            "  1. Open Signal on the source device.\n"
            "  2. Go to Settings > Chats > Chat Backups.\n"
            "  3. Note your 30-digit backup passphrase.\n"
            "  4. On the destination device, install Signal and choose "
            "'Restore from backup' during setup, then supply the passphrase.",
            "\n".join(f"  {p}" for p in backup_paths),
        )
    else:
        logger.info(
            "[signal/android] No Signal backup files found on shared storage. "
            "If you have Signal Chat Backups enabled, ensure the backup "
            "location is accessible (check Settings > Chats > Chat Backups)."
        )

    # ------------------------------------------------------------------
    # Step 3: Optionally stage the raw (encrypted) DB for future use
    # ------------------------------------------------------------------
    if is_rooted:
        _stage_encrypted_db(serial, sub, adb)

    # ------------------------------------------------------------------
    # Step 4: Report sqlcipher3 availability for future integration
    # ------------------------------------------------------------------
    if not _SQLCIPHER_AVAILABLE:
        logger.debug(
            "[signal/android] sqlcipher3 is not installed. "
            "It will be required if Signal ever exposes its SQLCipher key. "
            "Install with: pip install sqlcipher3"
        )

    # Honest return: no messages extracted
    return []


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def _detect_installation(serial: str, adb: ADBManager) -> bool:
    """
    Return True if the Signal package is present on the device.
    Uses 'pm list packages' which does not require root.
    """
    stdout, _, rc = adb.shell(
        serial,
        "pm list packages | grep signal",
        timeout=20,
    )
    if rc != 0:
        # rc != 0 from grep means no match (or adb failure)
        return False
    return _SIGNAL_PACKAGE in stdout


def _find_backup_files(serial: str, adb: ADBManager) -> list[str]:
    """
    Search common Signal backup locations for *.backup files.
    Returns a (possibly empty) list of device-side paths.
    """
    search_dirs = " ".join(_BACKUP_SEARCH_PATHS)
    stdout, _, rc = adb.shell(
        serial,
        f"find {search_dirs} -name '*.backup' 2>/dev/null",
        timeout=30,
    )
    if rc not in (0, 1):
        # rc 1 = find ran but no matches; other codes indicate an error
        logger.debug(
            "[signal/android] 'find' for Signal backups returned rc=%d", rc
        )
    paths = [
        line.strip()
        for line in stdout.splitlines()
        if line.strip().endswith(".backup")
    ]
    return paths


# ---------------------------------------------------------------------------
# Staging the encrypted DB (rooted path)
# ---------------------------------------------------------------------------

def _stage_encrypted_db(serial: str, sub: Path, adb: ADBManager) -> None:
    """
    Pull the encrypted signal.db to staging for potential future decryption.

    NOTE: The file cannot currently be opened without the SQLCipher key,
    which is held exclusively by the Android Keystore and is inaccessible
    even with root on Android 9+.  This step is purely for archival /
    future-proofing purposes.
    """
    local_db = sub / "signal.db"

    _, _, rc = adb.shell_root(
        serial,
        f"cp {_DB_REMOTE_SRC} {_DB_REMOTE_TMP}",
        timeout=30,
    )
    if rc != 0:
        logger.warning(
            "[signal/android] Could not copy signal.db to /sdcard/ (rc=%d). "
            "The database may not exist if Signal has never been launched, "
            "or the path changed in a newer Signal version. "
            "Expected path: %s",
            rc,
            _DB_REMOTE_SRC,
        )
        return

    adb.shell_root(serial, f"chmod 644 {_DB_REMOTE_TMP}", timeout=10)
    ok = adb.pull_verified(serial, _DB_REMOTE_TMP, local_db, timeout=120)
    adb.shell(serial, f"rm -f {_DB_REMOTE_TMP}", timeout=10)

    if ok and local_db.exists():
        logger.warning(
            "[signal/android] Signal database is protected by Android Keystore. "
            "The encrypted file has been staged at %s for reference, but "
            "the decryption key is NOT accessible even with root on modern "
            "Android. Manual export via Signal's built-in backup feature "
            "(Settings > Chats > Chat Backups) is the only supported path.",
            local_db,
        )
    else:
        logger.warning(
            "[signal/android] Failed to pull signal.db to staging. "
            "The file may be locked by the Signal process — "
            "try force-stopping Signal first: "
            "adb shell am force-stop %s",
            _SIGNAL_PACKAGE,
        )
