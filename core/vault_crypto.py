"""
core/vault_crypto.py

AES-256 encryption/decryption for vault ZIP files.

Two on-disk formats are supported:

  Version 1 — AES-256-GCM (legacy, read-only)
  ─────────────────────────────────────────────
  Offset  Size  Description
  ──────  ────  ─────────────────────────────────────────────────────────────
       0     4  Magic: b"PTVE"
       4     1  Version: 0x01
       5    16  PBKDF2 salt
      21    12  AES-GCM nonce
      33     N  Ciphertext + 16-byte GCM auth tag

  All data loaded into RAM at once — only viable for small vaults.
  New vaults are never written in v1.

  Version 2 — AES-256-CTR + HMAC-SHA256 (streaming, default)
  ────────────────────────────────────────────────────────────
  Offset  Size  Description
  ──────  ────  ─────────────────────────────────────────────────────────────
       0     4  Magic: b"PTVE"
       4     1  Version: 0x02
       5    16  PBKDF2 salt
      21    16  AES-CTR nonce (full 128-bit block)
      37     N  Ciphertext  (AES-256-CTR stream)
    37+N    32  HMAC-SHA256 over (salt || nonce || ciphertext)

  Two separate 32-byte keys are derived from one PBKDF2 pass (64 bytes
  total): the first 32 bytes are the encryption key, the next 32 bytes
  are the HMAC key.  Files are processed 1 MB at a time so peak RAM
  usage is O(chunk) regardless of vault size.

Key derivation: PBKDF2-HMAC-SHA256 · 480 000 iterations.
Chunk size: 1 MiB (configurable via _CHUNK).

Usage
-----
    from core.vault_crypto import encrypt_vault, decrypt_vault, is_encrypted

    # Encrypt an existing vault ZIP (streaming, safe for multi-GB vaults)
    encrypt_vault(Path("backup.zip"), password="s3cret")

    # Decrypt before reading
    decrypt_vault(Path("backup.enc"), Path("backup.zip"), password="s3cret")

    # Check whether a file is an encrypted vault
    if is_encrypted(path):
        decrypt_vault(path, tmp, password=user_password)

Requirements
------------
    pip install cryptography
"""

from __future__ import annotations

import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File format constants
# ---------------------------------------------------------------------------

_MAGIC          = b"PTVE"
_VERSION_V1     = b"\x01"
_VERSION_V2     = b"\x02"

_SALT_LEN       = 16
_V1_NONCE_LEN   = 12   # AES-GCM nonce
_V2_NONCE_LEN   = 16   # AES-CTR nonce (full block size)
_V2_MAC_LEN     = 32   # HMAC-SHA256

# v1 header: magic(4) + version(1) + salt(16) + nonce(12) = 33 bytes
_V1_HEADER_LEN  = 4 + 1 + _SALT_LEN + _V1_NONCE_LEN
# v2 header: magic(4) + version(1) + salt(16) + nonce(16) = 37 bytes
_V2_HEADER_LEN  = 4 + 1 + _SALT_LEN + _V2_NONCE_LEN

_KDF_ITERATIONS = 480_000
_KEY_LEN        = 32   # AES-256

_CHUNK          = 1 * 1024 * 1024   # 1 MiB — tune if needed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_encrypted(path: Path) -> bool:
    """Return True if *path* starts with the PTVE magic bytes."""
    try:
        with path.open("rb") as f:
            magic = f.read(4)
        return magic == _MAGIC
    except OSError:
        return False


def encrypt_vault(
    plain_path: Path,
    password: str,
    output_path: Path | None = None,
) -> Path:
    """
    Encrypt *plain_path* (a vault ZIP) with *password* using the v2
    streaming format.

    Parameters
    ----------
    plain_path:
        Source vault ZIP file.
    password:
        User-supplied passphrase (any length).
    output_path:
        Destination for the encrypted file.  If None, appends ``.enc``
        to *plain_path* (in-place: plain file is deleted after success).

    Returns
    -------
    Path to the encrypted vault file.
    """
    _require_cryptography()

    in_place = output_path is None
    if output_path is None:
        output_path = plain_path.with_suffix(plain_path.suffix + ".enc")

    _encrypt_v2(plain_path, password, output_path)

    size_mb = output_path.stat().st_size / 1_048_576
    logger.info(
        "vault_crypto: encrypted %s → %s (%.1f MB, v2/streaming)",
        plain_path.name, output_path.name, size_mb,
    )

    if in_place:
        plain_path.unlink()

    return output_path


def decrypt_vault(
    enc_path: Path,
    output_path: Path,
    password: str,
) -> Path:
    """
    Decrypt an encrypted vault file produced by :func:`encrypt_vault`.

    Supports both v1 (legacy GCM) and v2 (streaming CTR+HMAC) formats.

    Parameters
    ----------
    enc_path:
        Source encrypted vault file.
    output_path:
        Destination path for the decrypted ZIP.
    password:
        Passphrase used during encryption.

    Returns
    -------
    Path to the decrypted vault ZIP (*output_path*).

    Raises
    ------
    ValueError
        If the file is not a valid encrypted vault or the password is wrong.
    """
    _require_cryptography()

    with enc_path.open("rb") as f:
        header_peek = f.read(5)   # magic(4) + version(1)

    if len(header_peek) < 5 or header_peek[:4] != _MAGIC:
        raise ValueError(f"Not an encrypted vault (bad magic): {enc_path}")

    version = header_peek[4:5]
    if version == _VERSION_V1:
        _decrypt_v1(enc_path, output_path, password)
    elif version == _VERSION_V2:
        _decrypt_v2(enc_path, output_path, password)
    else:
        raise ValueError(f"Unsupported vault encryption version: {version!r}")

    logger.info("vault_crypto: decrypted %s → %s", enc_path.name, output_path.name)
    return output_path


def change_password(
    enc_path: Path,
    old_password: str,
    new_password: str,
) -> Path:
    """
    Re-encrypt a vault with a new password without writing plaintext to disk.

    Decrypts to a temporary in-memory buffer and re-encrypts.  For very
    large vaults on memory-constrained machines prefer decrypt → re-encrypt
    via :func:`decrypt_vault` + :func:`encrypt_vault` instead.

    Returns the same *enc_path*.
    """
    _require_cryptography()

    import tempfile

    with tempfile.NamedTemporaryFile(
        dir=enc_path.parent, suffix=".plain_tmp", delete=False
    ) as tf:
        tmp_plain = Path(tf.name)

    try:
        decrypt_vault(enc_path, tmp_plain, old_password)
        tmp_enc = enc_path.with_suffix(".new_tmp")
        _encrypt_v2(tmp_plain, new_password, tmp_enc)
        tmp_enc.replace(enc_path)
    finally:
        if tmp_plain.exists():
            tmp_plain.unlink()

    logger.info("vault_crypto: password changed for %s", enc_path.name)
    return enc_path


# ---------------------------------------------------------------------------
# v2 streaming implementation (AES-256-CTR + HMAC-SHA256)
# ---------------------------------------------------------------------------

def _derive_keys_v2(password: str, salt: bytes) -> tuple[bytes, bytes]:
    """Derive enc_key (32 B) and mac_key (32 B) from password + salt."""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_LEN * 2,   # 64 bytes → two 32-byte keys
        salt=salt,
        iterations=_KDF_ITERATIONS,
    )
    km = kdf.derive(password.encode("utf-8"))
    return km[:_KEY_LEN], km[_KEY_LEN:]


def _encrypt_v2(plain_path: Path, password: str, output_path: Path) -> None:
    """
    Stream-encrypt *plain_path* into *output_path* using AES-256-CTR +
    HMAC-SHA256 (Encrypt-then-MAC).  Peak RAM usage ≈ 2 × _CHUNK.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.hmac import HMAC as _HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.backends import default_backend

    salt     = os.urandom(_SALT_LEN)
    nonce    = os.urandom(_V2_NONCE_LEN)
    enc_key, mac_key = _derive_keys_v2(password, salt)

    encryptor = Cipher(
        algorithms.AES(enc_key), modes.CTR(nonce), backend=default_backend()
    ).encryptor()
    mac = _HMAC(mac_key, hashes.SHA256(), backend=default_backend())
    mac.update(salt)
    mac.update(nonce)

    with plain_path.open("rb") as fin, output_path.open("wb") as fout:
        fout.write(_MAGIC)
        fout.write(_VERSION_V2)
        fout.write(salt)
        fout.write(nonce)

        while True:
            chunk = fin.read(_CHUNK)
            if not chunk:
                break
            ct = encryptor.update(chunk)
            mac.update(ct)
            fout.write(ct)

        ct_final = encryptor.finalize()
        if ct_final:
            mac.update(ct_final)
            fout.write(ct_final)

        fout.write(mac.finalize())


def _decrypt_v2(enc_path: Path, output_path: Path, password: str) -> None:
    """
    Stream-decrypt a v2 vault file.

    Two-pass approach for authenticate-then-decrypt:
      Pass 1 — compute HMAC over the ciphertext and verify it.
      Pass 2 — decrypt the verified ciphertext chunk by chunk.

    Neither pass loads more than _CHUNK bytes into RAM at a time.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.hmac import HMAC as _HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.backends import default_backend
    from cryptography.exceptions import InvalidSignature

    file_size = enc_path.stat().st_size
    ct_size   = file_size - _V2_HEADER_LEN - _V2_MAC_LEN
    if ct_size < 0:
        raise ValueError(f"File too short to be a v2 encrypted vault: {enc_path}")

    with enc_path.open("rb") as f:
        magic   = f.read(4)
        version = f.read(1)
        salt    = f.read(_SALT_LEN)
        nonce   = f.read(_V2_NONCE_LEN)
        # f is now positioned at the start of the ciphertext

        if magic != _MAGIC or version != _VERSION_V2:
            raise ValueError("Not a v2 encrypted vault")

        enc_key, mac_key = _derive_keys_v2(password, salt)

        # ── Pass 1: verify HMAC ────────────────────────────────────────────
        mac = _HMAC(mac_key, hashes.SHA256(), backend=default_backend())
        mac.update(salt)
        mac.update(nonce)

        remaining = ct_size
        while remaining > 0:
            chunk = f.read(min(_CHUNK, remaining))
            mac.update(chunk)
            remaining -= len(chunk)

        stored_hmac = f.read(_V2_MAC_LEN)

    try:
        mac.verify(stored_hmac)
    except InvalidSignature:
        raise ValueError("Incorrect password or corrupted vault") from None

    # ── Pass 2: decrypt ────────────────────────────────────────────────────
    decryptor = Cipher(
        algorithms.AES(enc_key), modes.CTR(nonce), backend=default_backend()
    ).decryptor()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with enc_path.open("rb") as fin, output_path.open("wb") as fout:
        fin.seek(_V2_HEADER_LEN)
        remaining = ct_size
        while remaining > 0:
            chunk = fin.read(min(_CHUNK, remaining))
            fout.write(decryptor.update(chunk))
            remaining -= len(chunk)
        fout.write(decryptor.finalize())


# ---------------------------------------------------------------------------
# v1 legacy implementation (AES-256-GCM, read-only)
# ---------------------------------------------------------------------------

def _derive_key_v1(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_LEN,
        salt=salt,
        iterations=_KDF_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


def _decrypt_v1(enc_path: Path, output_path: Path, password: str) -> None:
    """Decrypt a legacy v1 (AES-256-GCM) vault.  Loads entire file into RAM."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag

    with enc_path.open("rb") as f:
        header     = f.read(_V1_HEADER_LEN)
        ciphertext = f.read()

    if len(header) < _V1_HEADER_LEN:
        raise ValueError(f"File too short to be a v1 encrypted vault: {enc_path}")

    salt  = header[5 : 5 + _SALT_LEN]
    nonce = header[5 + _SALT_LEN : 5 + _SALT_LEN + _V1_NONCE_LEN]
    key   = _derive_key_v1(password, salt)

    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag:
        raise ValueError("Incorrect password or corrupted vault") from None

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(plaintext)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_cryptography() -> None:
    try:
        import cryptography  # noqa: F401
    except ImportError:
        raise ImportError(
            "vault_crypto requires the 'cryptography' package. "
            "Install it with: pip install cryptography"
        ) from None
