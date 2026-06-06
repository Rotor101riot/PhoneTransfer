"""
inject_signal_android.py
Stub injector for Signal messages on Android.

Why injection is not supported
--------------------------------
Restoring a Signal database to an Android device is not a simple file-copy
operation.  The following hard constraints make it unsupportable as a
general-purpose tool:

1. SQLCipher encryption with a per-installation key.
   Signal Android's database (signal.db / signal.sqlite) is encrypted with
   SQLCipher.  The encryption key is generated once during Signal's first-run
   setup and stored in Signal's private data directory
   (/data/data/org.thoughtcrime.securesms/).  That directory is inaccessible
   without root on the *target* device.

2. Root access required — on both source and destination.
   Extracting the key from the source device requires root.  Writing the
   database and key to the target device also requires root.  Consumer devices
   are not rooted by default and enabling root voids the warranty / disables
   Android's Verified Boot attestation.

3. Exact version match.
   Signal applies database migrations on startup.  If the source and target
   Signal versions differ, Signal will refuse to open the database or silently
   corrupt data during migration.  The schema version is embedded in the
   database and must match the installed APK.

4. Account binding.
   Each Signal installation is bound to a registered phone number and a set of
   identity keys stored on Signal's servers.  Copying a database to a device
   registered to a different account (or the same account re-registered) will
   cause Signal to detect a key mismatch and refuse to display messages, or
   will trigger a safety-number change that alerts all contacts.

5. Signal's recommended path.
   Signal provides an official encrypted backup/restore mechanism
   (Settings → Chats → Chat Backups) and a QR-code-based device-link flow.
   These are the only supported migration paths.

This module exists so that the injector registry can include Signal Android
without crashing; it always returns 0 and logs a clear explanation.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def inject(device_id: str, items: list, staging_dir: Path, is_privileged: bool) -> int:
    """
    Signal Android injection is not supported.

    Injecting Signal messages requires root access on both source and target
    devices, an exact Signal version match, and a compatible account
    registration.  None of these conditions can be guaranteed or safely
    automated by a third-party tool.

    Parameters
    ----------
    device_id:
        Ignored.
    items:
        Ignored.
    staging_dir:
        Ignored.
    is_privileged:
        Ignored.  Even with root access the account-binding and version-match
        constraints remain unsolved.

    Returns
    -------
    int
        Always 0.
    """
    logger.warning(
        "Signal Android: injection is not supported. "
        "Restoring a Signal database to a different Android device requires: "
        "(1) root access on both the source and target device to read/write "
        "Signal's private data directory; "
        "(2) an exact match between the installed Signal APK version and the "
        "database schema version of the source backup; "
        "(3) a compatible account registration — copying a database registered "
        "to one account onto a device registered to another causes Signal to "
        "detect an identity-key mismatch and refuse to display messages. "
        "Use Signal's built-in encrypted backup/restore "
        "(Settings -> Chats -> Chat Backups) or the device-link QR-code flow instead."
    )
    return 0
