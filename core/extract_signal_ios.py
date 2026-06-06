"""
extract_signal_ios.py
Stub extractor for Signal messages on iOS.

Why extraction is not possible
-------------------------------
Signal on iOS stores its message database (signal.sqlite) encrypted with
SQLCipher.  The encryption key is derived from material held in the device's
Secure Enclave (a dedicated security co-processor inside the Apple SoC).

The Secure Enclave is designed so that its key material never leaves the chip
in plaintext — not via JTAG, not via DFU, not via any AFC or lockdown
service, and not via a jailbreak.  Even on a fully jailbroken device the
Secure Enclave remains opaque to software running on the Application
Processor.

Practical consequences:
  * The SQLCipher passphrase cannot be recovered without breaking the Secure
    Enclave, which is not possible with any publicly known technique.
  * iTunes / iCloud encrypted backups include the encrypted database file but
    not the Secure Enclave-derived key, so offline decryption is also not
    possible.
  * Signal intentionally does not expose an export API to prevent exactly this
    kind of extraction.

The only supported migration path for Signal iOS data is Signal's built-in
"Transfer or Reset iPhone" flow, which uses a direct device-to-device
encrypted channel.

This module exists so that the extractor registry can include Signal iOS
without crashing; it always returns an empty list and logs a clear
explanation.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def extract(device_id: str, staging_dir: Path, is_privileged: bool) -> list:
    """
    Signal iOS extraction is not supported.

    Signal's message database is encrypted with SQLCipher using a key derived
    from the device's Secure Enclave.  The key cannot be extracted from the
    Secure Enclave by any software means — including on jailbroken devices —
    making decryption impossible outside the originating device.

    Parameters
    ----------
    device_id:
        Ignored.
    staging_dir:
        Ignored.
    is_privileged:
        Ignored.  Jailbreak status does not affect the Secure Enclave's
        key-isolation guarantee.

    Returns
    -------
    list
        Always an empty list.
    """
    logger.warning(
        "Signal iOS: extraction is not supported. "
        "Signal's database is encrypted with SQLCipher using a key derived from "
        "the device's Secure Enclave. The Secure Enclave does not expose key "
        "material to any software running on the Application Processor, including "
        "jailbreak-level code. Decryption outside the originating device is not "
        "possible. Use Signal's built-in device-to-device transfer feature instead."
    )
    return []
