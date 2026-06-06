"""
inject_signal_ios.py
Stub injector for Signal messages on iOS.

Why injection is not supported
--------------------------------
Signal on iOS does not provide any public API for importing messages.
The app's SQLCipher database is encrypted with a key that is bound to the
Secure Enclave of the specific device on which Signal is installed.

Even if a decrypted database from another device could be obtained (it
cannot — see extract_signal_ios.py), writing it back to a different device
would require:
  1. Generating a new SQLCipher key tied to the target device's Secure
     Enclave — which requires Signal's own internal code paths.
  2. Bypassing Signal's data-protection class (Complete Protection /
     NSFileProtectionComplete) to write into Signal's sandboxed container.
  3. Signal performing its own database migration and verification on startup,
     which it will refuse to do if the database origin does not match the
     registered account.

None of these steps are achievable by a third-party tool.

The only supported migration path is Signal's built-in
"Transfer or Reset iPhone" flow.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def inject(device_id: str, items: list, staging_dir: Path, is_privileged: bool) -> int:
    """
    Signal iOS injection is not supported.

    Parameters
    ----------
    device_id:
        Ignored.
    items:
        Ignored.
    staging_dir:
        Ignored.
    is_privileged:
        Ignored.

    Returns
    -------
    int
        Always 0.
    """
    logger.warning(
        "Signal iOS: injection is not supported. "
        "Signal's database is bound to the Secure Enclave of the specific device "
        "on which the app is installed. There is no mechanism to import a foreign "
        "message database into Signal on iOS. Use Signal's built-in device-to-device "
        "transfer feature instead."
    )
    return 0
