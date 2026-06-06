"""
iosbackup_helpers.py

Utility helpers for working around iOSbackup library quirks.

Problem
-------
iOSbackup's ``getFileDecryptedCopy`` truncates the decrypted output file to
``info['size']``, where ``info['size']`` comes from the backup Manifest.db.
When a backup was created by iMazing the Size field in Manifest.db is often
stale (smaller than the real plaintext).  This causes SQLite files to be
truncated mid-page, resulting in "database disk image is malformed" errors.

The correct plaintext length is always ``len(encrypted_blob) - 16`` — the
AES-CBC block used for PKCS7 padding is stripped but the iOSbackup code
subtracts nothing extra; the true padding is exactly the last 16-byte block.

Fix
---
``fix_truncated_sqlite(dest, backup, relative_path)`` checks the decrypted
file's SQLite header and, if the file is shorter than the page count implies,
re-decrypts the blob from scratch with the correct truncation.
"""

from __future__ import annotations

import logging
import os
import struct
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # iOSbackup is not typed; avoid circular imports

logger = logging.getLogger(__name__)

_SQLITE_MAGIC = b"SQLite format 3\x00"


def _sqlite_expected_size(path: Path) -> int | None:
    """
    Return the expected size of a SQLite database from its header.

    Returns ``None`` if the file is not a recognisable SQLite file or if the
    header cannot be read.
    """
    try:
        with open(path, "rb") as f:
            header = f.read(32)
    except OSError:
        return None

    if len(header) < 32 or header[:16] != _SQLITE_MAGIC:
        return None

    # Bytes 16-17: page size (big-endian uint16; value 1 → 65536)
    raw_page_size = struct.unpack(">H", header[16:18])[0]
    page_size = 65536 if raw_page_size == 1 else raw_page_size
    if page_size < 512:
        return None

    # Bytes 28-31: page count (big-endian uint32, valid when file format ≥ 4)
    page_count = struct.unpack(">I", header[28:32])[0]
    if page_count == 0:
        return None

    return page_size * page_count


def fix_truncated_sqlite(dest: Path, backup: object, relative_path: str) -> None:
    """
    Re-decrypt *relative_path* from *backup* if the SQLite file at *dest* is
    shorter than its header-declared size.

    Parameters
    ----------
    dest:          Path to the already-written (possibly truncated) SQLite file.
    backup:        An ``iOSbackup.iOSbackup`` instance with ``backupRoot`` and
                   ``udid`` attributes and the ``unwrapKeyForClass`` method.
    relative_path: The relative backup path used to fetch the file (e.g.
                   ``"Library/SMS/sms.db"``).
    """
    expected = _sqlite_expected_size(dest)
    if expected is None:
        return  # not a SQLite file or header unreadable

    actual = dest.stat().st_size if dest.exists() else 0
    if actual >= expected:
        return  # file is not truncated

    logger.debug(
        "fix_truncated_sqlite: %s is %d bytes but header says %d; re-decrypting",
        dest.name,
        actual,
        expected,
    )

    try:
        _redecrypt(dest, backup, relative_path)
    except Exception as exc:
        logger.warning(
            "fix_truncated_sqlite: re-decryption of %s failed: %s", dest.name, exc
        )


def _redecrypt(dest: Path, backup: object, relative_path: str) -> None:
    """Core re-decryption logic (separated for clean traceback isolation)."""
    from iOSbackup import iOSbackup as _iOSBackupCls  # type: ignore[import]
    from Crypto.Cipher import AES  # type: ignore[import]

    # Retrieve manifest entry — same call iOSbackup makes internally.
    manifest_entry = backup.getFileManifestDBEntry(relativePath=relative_path)  # type: ignore[attr-defined]
    if not manifest_entry:
        raise RuntimeError(f"manifest entry not found for {relative_path!r}")

    file_id: str = manifest_entry["fileID"]
    file_info = _iOSBackupCls.getFileInfo(manifest_entry["manifest"])
    manifest = file_info["completeManifest"]

    # Navigate the NSKeyedArchiver structure to reach the actual file dict.
    if "$version" in manifest:
        file_data = manifest["$objects"][1]
    else:
        file_data = manifest

    if "EncryptionKey" not in file_data:
        raise RuntimeError("file is not encrypted; cannot re-decrypt")

    encryption_key = file_data["EncryptionKey"][4:]  # strip 4-byte length prefix
    key = backup.unwrapKeyForClass(file_data["ProtectionClass"], encryption_key)  # type: ignore[attr-defined]

    blob_path = os.path.join(
        backup.backupRoot,  # type: ignore[attr-defined]
        backup.udid,  # type: ignore[attr-defined]
        file_id[:2],
        file_id,
    )
    correct_size = os.path.getsize(blob_path) - 16

    decryptor = AES.new(key, AES.MODE_CBC, b"\x00" * 16)
    chunk_size = 16 * 1_000_000  # 16 MB — matches iOSbackup default

    with open(blob_path, "rb") as in_file, open(str(dest), "wb") as out_file:
        while True:
            chunk = in_file.read(chunk_size)
            if not chunk:
                break
            out_file.write(decryptor.decrypt(chunk))
        out_file.truncate(correct_size)

    logger.debug(
        "fix_truncated_sqlite: %s re-decrypted to %d bytes (was %d)",
        dest.name,
        correct_size,
        dest.stat().st_size if dest.exists() else 0,
    )
