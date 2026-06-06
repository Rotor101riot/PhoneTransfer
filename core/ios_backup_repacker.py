"""
ios_backup_repacker.py

Reusable, class-based re-packer for encrypted iOS (MobileSync) backups.

Ported from the G:/test/repack_backup.py proof-of-concept so each of the
iOS injector modules (inject_sms_ios, inject_calls_ios, ...) can stage
overrides / additions / deletions against a single backup and commit them
in one pass at the end of the destination pipeline.

High-level flow:

    repacker = IOSBackupRepacker(source_backup_dir, passphrase)
    repacker.unlock()

    # Extract the live DB into staging so an injector can mutate it:
    sms_db_bytes = repacker.extract_file("HomeDomain", "Library/SMS/sms.db")
    # ... injector opens sms.db, INSERTs rows, writes back ...

    # Stage the modified DB as an override:
    repacker.stage_override("HomeDomain", "Library/SMS/sms.db", new_db_path)

    # Stage a new file (photo, voicemail audio, ...):
    repacker.stage_addition(
        "CameraRollDomain",
        "Media/DCIM/100APPLE/IMG_0001.JPG",
        jpeg_path,
    )

    # Commit: mirror source -> output, re-encrypt blobs, rebuild Manifest.db:
    stats = repacker.commit(output_dir)

The three staging methods are idempotent — calling them twice for the same
(domain, relativePath) replaces the earlier pending change.  `commit` may
only be called once per instance.

Crypto is copied verbatim from the reference implementation: AES-CBC with
a zero IV and PKCS7 padding for per-file encryption, RFC 3394 AES Key Wrap
for wrapping freshly-generated per-file keys against a keybag class key.

Protection classes for *additions* follow a priority-ordered rules list
(default: class 3 NSFileProtectionNone, class 4 for Library/Voicemail/*).
Callers may override per call via ``protection_class=``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import plistlib
import secrets
import shutil
import sqlite3
import struct
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import Crypto.Cipher.AES

from iphone_backup_decrypt import EncryptedBackup
from iphone_backup_decrypt.iphone_backup import utils
from iphone_backup_decrypt import google_iphone_dataprotection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_PROTECTION_CLASS = 3  # NSFileProtectionNone (Camera Roll photos)

# (domain, fnmatch-glob on relativePath, protection_class).  First match wins.
DEFAULT_PROTECTION_RULES: list[tuple[str, str, int]] = [
    ("HomeDomain", "Library/Voicemail/*", 4),
]

# Valid iOS data-protection classes for backup files.  The keybag in a
# passphrase-encrypted backup always contains class keys for 1–4 (the four
# NIST-specified data-protection classes).  5–12 are less common and may be
# absent in older keybags.
_VALID_PROTECTION_CLASSES: frozenset[int] = frozenset(range(1, 13))

# Classes that require the device to be actively unlocked to decrypt the file.
# Assigning class 1 (NSFileProtectionComplete) or class 2
# (NSFileProtectionCompleteUnlessOpen) to a file read at first-boot time
# causes a silent failure: the backup restores cleanly, but the device can't
# open the file until the user enters their passcode after reboot.
_BOOT_SENSITIVE_CLASSES: frozenset[int] = frozenset({1, 2})

_BOOT_SENSITIVE_CLASS_NAMES: dict[int, str] = {
    1: "NSFileProtectionComplete",
    2: "NSFileProtectionCompleteUnlessOpen",
}


# ---------------------------------------------------------------------------
# Crypto primitives
# ---------------------------------------------------------------------------

def _pad_pkcs7(data: bytes, block_size: int = 16) -> bytes:
    n = block_size - (len(data) % block_size)
    return data + bytes([n]) * n


def _decrypt_without_size_check(backup, file_id: str, file_bplist: bytes) -> bytes:
    """
    Drop-in for ``EncryptedBackup._decrypt_inner_file`` that skips the strict
    ``size == plaintext length`` assertion.  The PKCS7 unpad still validates
    structural integrity; the size mismatch is cosmetic for our use case
    (we're re-encrypting the file anyway, not trusting the old Size field).
    """
    backup._read_and_unlock_keybag()
    fp = utils.FilePlist(file_bplist)
    if fp.encryption_key is None:
        raise ValueError("Not an encrypted file (directory or empty).")
    inner_key = backup._keybag.unwrapKeyForClass(
        fp.protection_class, fp.encryption_key
    )
    blob_path = os.path.join(backup._backup_directory, file_id[:2], file_id)
    with open(blob_path, "rb") as fh:
        encrypted = fh.read()
    decrypted = google_iphone_dataprotection.AESdecryptCBC(encrypted, inner_key)
    return google_iphone_dataprotection.removePadding(decrypted)


def _aes_encrypt_cbc(plaintext: bytes, key: bytes, iv: bytes = b"\x00" * 16) -> bytes:
    cipher = Crypto.Cipher.AES.new(key, Crypto.Cipher.AES.MODE_CBC, iv)
    return cipher.encrypt(_pad_pkcs7(plaintext))


def _aes_key_wrap(kek: bytes, plaintext: bytes) -> bytes:
    """RFC 3394 AES Key Wrap — inverse of iphone_backup_decrypt._AESUnwrap."""
    assert len(plaintext) % 8 == 0, "wrap input must be 8-byte aligned"
    n = len(plaintext) // 8
    A = 0xA6A6A6A6A6A6A6A6
    R = [plaintext[i * 8:(i + 1) * 8] for i in range(n)]
    cipher = Crypto.Cipher.AES.new(kek, Crypto.Cipher.AES.MODE_ECB)
    for j in range(6):
        for i in range(n):
            B = cipher.encrypt(struct.pack(">Q", A) + R[i])
            A = struct.unpack(">Q", B[:8])[0] ^ ((n * j) + i + 1)
            R[i] = B[8:]
    return struct.pack(">Q", A) + b"".join(R)


def _stream_encrypt_and_digest(
    src: Path, dst: Path, key: bytes, chunk_size: int = 1 << 20
) -> tuple[int, bytes]:
    """Encrypt src -> dst in streaming chunks; return (plaintext_size, sha1(ciphertext))."""
    cipher = Crypto.Cipher.AES.new(key, Crypto.Cipher.AES.MODE_CBC, b"\x00" * 16)
    sha1 = hashlib.sha1()
    plaintext_size = 0
    pending = b""

    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            buf = fin.read(chunk_size)
            if not buf:
                break
            plaintext_size += len(buf)
            data = pending + buf
            usable = len(data) - (len(data) % 16)
            if usable:
                ct = cipher.encrypt(data[:usable])
                fout.write(ct)
                sha1.update(ct)
            pending = data[usable:]
        final = cipher.encrypt(_pad_pkcs7(pending))
        fout.write(final)
        sha1.update(final)

    return plaintext_size, sha1.digest()


# ---------------------------------------------------------------------------
# bplist helpers
# ---------------------------------------------------------------------------

def _update_file_bplist(bplist_bytes: bytes, new_size: int, new_digest: bytes) -> bytes:
    """Patch an existing MBFile NSKeyedArchiver bplist's Size + Digest in place."""
    p = plistlib.loads(bplist_bytes)
    root_uid = p["$top"]["root"].data
    root_obj = p["$objects"][root_uid]
    root_obj["Size"] = new_size

    digest_ref = root_obj.get("Digest")
    if digest_ref is not None:
        digest_uid = digest_ref.data
        existing = p["$objects"][digest_uid]
        if isinstance(existing, dict) and "NS.data" in existing:
            existing["NS.data"] = new_digest
        else:
            p["$objects"][digest_uid] = new_digest

    return plistlib.dumps(p, fmt=plistlib.FMT_BINARY)


def _build_new_file_bplist(
    *,
    relative_path: str,
    size: int,
    digest: bytes,
    encryption_key_blob: bytes,
    protection_class: int,
    inode: int,
) -> bytes:
    """Fresh MBFile bplist for a NEW Files row (flags=1)."""
    now = int(time.time())
    nsmutabledata_class = {
        "$classname": "NSMutableData",
        "$classes": ["NSMutableData", "NSData", "NSObject"],
    }
    encryption_key_wrapper = {
        "NS.data": encryption_key_blob,
        "$class": plistlib.UID(4),
    }
    digest_wrapper = {
        "NS.data": digest,
        "$class": plistlib.UID(4),
    }
    mbfile_class = {
        "$classname": "MBFile",
        "$classes": ["MBFile", "NSObject"],
    }
    root_obj = {
        "$class": plistlib.UID(6),
        "RelativePath": plistlib.UID(2),
        "EncryptionKey": plistlib.UID(3),
        "Digest": plistlib.UID(5),
        "Size": size,
        "Mode": 33188,
        "UserID": 501,
        "GroupID": 501,
        "Flags": 0,
        "ProtectionClass": protection_class,
        "Birth": now,
        "LastModified": now,
        "LastStatusChange": now,
        "InodeNumber": inode,
    }
    p = {
        "$version": 100000,
        "$archiver": "NSKeyedArchiver",
        "$top": {"root": plistlib.UID(1)},
        "$objects": [
            "$null",
            root_obj,
            relative_path,
            encryption_key_wrapper,
            nsmutabledata_class,
            digest_wrapper,
            mbfile_class,
        ],
    }
    return plistlib.dumps(p, fmt=plistlib.FMT_BINARY)


def _build_new_directory_bplist(*, relative_path: str, inode: int) -> bytes:
    """Fresh MBFile bplist for a NEW directory row (flags=2)."""
    now = int(time.time())
    mbfile_class = {
        "$classname": "MBFile",
        "$classes": ["MBFile", "NSObject"],
    }
    root_obj = {
        "$class": plistlib.UID(3),
        "RelativePath": plistlib.UID(2),
        "Size": 0,
        "Mode": 16877,
        "UserID": 501,
        "GroupID": 501,
        "Flags": 0,
        "ProtectionClass": 0,
        "Birth": now,
        "LastModified": now,
        "LastStatusChange": now,
        "InodeNumber": inode,
    }
    p = {
        "$version": 100000,
        "$archiver": "NSKeyedArchiver",
        "$top": {"root": plistlib.UID(1)},
        "$objects": [
            "$null",
            root_obj,
            relative_path,
            mbfile_class,
        ],
    }
    return plistlib.dumps(p, fmt=plistlib.FMT_BINARY)


def _make_file_id(domain: str, relative_path: str) -> str:
    """Apple's documented backup fileID: sha1(domain + '-' + relativePath)."""
    return hashlib.sha1(f"{domain}-{relative_path}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Staging dataclasses
# ---------------------------------------------------------------------------

@dataclass
class _Override:
    domain: str
    relative_path: str
    local_path: Path


@dataclass
class _Addition:
    domain: str
    relative_path: str
    local_path: Path
    protection_class: int | None  # None = derive from rules


@dataclass
class _Deletion:
    domain: str
    relative_path: str


@dataclass
class RepackStats:
    overrides: int = 0
    additions: int = 0
    additions_bytes: int = 0
    deletions: int = 0
    deletions_missing: int = 0
    directories_added: int = 0
    duration_seconds: float = 0.0
    output_dir: Path = field(default_factory=Path)


# ---------------------------------------------------------------------------
# Repacker
# ---------------------------------------------------------------------------

class IOSBackupRepacker:
    """Re-pack an encrypted iOS backup with staged overrides/additions/deletions."""

    def __init__(
        self,
        source_dir: str | Path,
        passphrase: str,
        *,
        default_protection_class: int = DEFAULT_PROTECTION_CLASS,
        protection_rules: list[tuple[str, str, int]] | None = None,
        scratch_dir: str | Path | None = None,
    ) -> None:
        self.source_dir = Path(source_dir)
        self.passphrase = passphrase
        self.default_protection_class = default_protection_class
        self.protection_rules = list(
            protection_rules if protection_rules is not None else DEFAULT_PROTECTION_RULES
        )

        # Scratch dir for staged-from-bytes payloads.  Cleared on close/commit.
        self._owns_scratch = scratch_dir is None
        if scratch_dir is None:
            self._scratch = Path(tempfile.mkdtemp(prefix="iosrepack_"))
        else:
            self._scratch = Path(scratch_dir)
            self._scratch.mkdir(parents=True, exist_ok=True)
        self._scratch_counter = 0

        # Staged mutations, keyed by (domain, relativePath) so duplicates overwrite.
        self._overrides: dict[tuple[str, str], _Override] = {}
        self._additions: dict[tuple[str, str], _Addition] = {}
        self._deletions: dict[tuple[str, str], _Deletion] = {}

        # Lazily populated in unlock().
        self._backup: EncryptedBackup | None = None
        self._manifest_db_path: Path | None = None
        self._manifest_key: bytes | None = None

        self._committed = False

    # -- lifecycle -------------------------------------------------------

    def unlock(self) -> None:
        """Open the source backup, unlock its keybag, and decrypt Manifest.db."""
        if self._backup is not None:
            return

        backup = EncryptedBackup(
            backup_directory=str(self.source_dir),
            passphrase=self.passphrase,
        )
        backup._read_and_unlock_keybag()
        backup._decrypt_manifest_db_file()

        manifest_plist = backup._manifest_plist
        manifest_class = struct.unpack("<l", manifest_plist["ManifestKey"][:4])[0]
        manifest_wrapped = manifest_plist["ManifestKey"][4:]
        self._manifest_key = backup._keybag.unwrapKeyForClass(
            manifest_class, manifest_wrapped
        )
        self._manifest_db_path = Path(backup._temp_decrypted_manifest_db_path)

        # NOTE: do NOT close backup._temp_manifest_db_conn here.  The library
        # needs it open for extract_file / extract_file_as_bytes to look up
        # fileIDs during staging.  We close it at the top of commit() instead,
        # right before opening our own connection for UPDATE.

        # The library's _decrypt_inner_file asserts that the decrypted plaintext
        # size exactly matches Size in the MBFile bplist.  On some real
        # backups the bplist Size disagrees with what actually decrypts (large
        # system DBs, WAL-coalesced files) and the assertion kills extraction.
        # Swap in a size-check-free variant — the PKCS7 unpad already tells us
        # the data is structurally valid.
        backup._decrypt_inner_file = (
            lambda *, file_id, file_bplist: _decrypt_without_size_check(
                backup, file_id, file_bplist
            )
        )

        self._backup = backup
        logger.info(
            "IOSBackupRepacker: unlocked %s (Manifest class=%d, key=%d bytes)",
            self.source_dir, manifest_class, len(self._manifest_key),
        )

    def close(self) -> None:
        """Release temp state.  Idempotent."""
        if self._owns_scratch and self._scratch.exists():
            try:
                shutil.rmtree(self._scratch, ignore_errors=True)
            except Exception:
                pass
        self._backup = None
        self._manifest_db_path = None
        self._manifest_key = None

    def __enter__(self) -> "IOSBackupRepacker":
        self.unlock()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- read side -------------------------------------------------------

    def extract_file(self, domain: str, relative_path: str) -> bytes:
        """Return decrypted bytes for (domain, relativePath) from the SOURCE backup."""
        self.unlock()
        assert self._backup is not None
        return self._backup.extract_file_as_bytes(
            relative_path, domain_like=domain
        )

    def extract_file_to(
        self, domain: str, relative_path: str, dst: str | Path
    ) -> Path:
        """Extract-and-write; returns the destination path."""
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(self.extract_file(domain, relative_path))
        return dst

    # -- staging ---------------------------------------------------------

    def stage_override(
        self,
        domain: str,
        relative_path: str,
        data: bytes | str | Path,
    ) -> None:
        """Override an existing Manifest.db Files row with new contents."""
        self._ensure_not_committed()
        local = self._materialize(data, _slug(domain, relative_path))
        self._overrides[(domain, relative_path)] = _Override(
            domain, relative_path, local
        )

    def stage_addition(
        self,
        domain: str,
        relative_path: str,
        data: bytes | str | Path,
        *,
        protection_class: int | None = None,
    ) -> None:
        """Add a NEW file to the backup (new Files row + new on-disk blob)."""
        self._ensure_not_committed()
        local = self._materialize(data, _slug(domain, relative_path))
        self._additions[(domain, relative_path)] = _Addition(
            domain, relative_path, local, protection_class
        )

    def stage_deletion(self, domain: str, relative_path: str) -> None:
        """Remove a file from the backup (deletes Files row + blob)."""
        self._ensure_not_committed()
        self._deletions[(domain, relative_path)] = _Deletion(domain, relative_path)

    # -- commit ----------------------------------------------------------

    def commit(self, output_dir: str | Path) -> RepackStats:
        """Mirror source -> output, apply all staged mutations, re-encrypt Manifest.db."""
        self._ensure_not_committed()
        self.unlock()
        assert self._backup is not None
        assert self._manifest_db_path is not None
        assert self._manifest_key is not None

        t0 = time.time()
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        stats = RepackStats(output_dir=output_dir)

        # 1. Mirror via hard links (or copy fallback).
        logger.info("IOSBackupRepacker: mirroring source to %s", output_dir)
        self._mirror_to(output_dir)

        # Release the library's shared connection now that no more staging
        # reads will happen — our own UPDATE connection needs exclusive access.
        if getattr(self._backup, "_temp_manifest_db_conn", None):
            self._backup._temp_manifest_db_conn.close()
            self._backup._temp_manifest_db_conn = None

        # 2. Apply mutations in-place against the decrypted Manifest.db.
        con = sqlite3.connect(str(self._manifest_db_path))
        try:
            self._apply_overrides(con, output_dir, stats)
            self._apply_deletions(con, output_dir, stats)
            self._apply_additions(con, output_dir, stats)
            con.commit()
        finally:
            con.close()

        # 3. Re-encrypt Manifest.db into the output.
        manifest_plain = self._manifest_db_path.read_bytes()
        manifest_cipher = _aes_encrypt_cbc(manifest_plain, self._manifest_key)
        out_manifest = output_dir / "Manifest.db"
        if out_manifest.exists():
            out_manifest.unlink()
        out_manifest.write_bytes(manifest_cipher)
        logger.info(
            "IOSBackupRepacker: rewrote Manifest.db (%d -> %d bytes)",
            len(manifest_plain), len(manifest_cipher),
        )

        stats.duration_seconds = time.time() - t0
        self._committed = True
        return stats

    # -- internals -------------------------------------------------------

    def _ensure_not_committed(self) -> None:
        if self._committed:
            raise RuntimeError("IOSBackupRepacker.commit() already called")

    def _materialize(self, data: bytes | str | Path, slug: str) -> Path:
        """Coerce `data` into a concrete Path under the scratch dir."""
        if isinstance(data, (bytes, bytearray)):
            self._scratch_counter += 1
            dst = self._scratch / f"{self._scratch_counter:05d}_{slug}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(bytes(data))
            return dst
        p = Path(data)
        if not p.is_absolute():
            p = p.resolve()
        if not p.exists():
            raise FileNotFoundError(f"staged payload missing: {p}")
        return p

    def _addition_protection_class(
        self, domain: str, relative_path: str, override: int | None
    ) -> int:
        if override is not None:
            return override
        for rule_domain, glob, cls in self.protection_rules:
            if rule_domain == domain and fnmatch.fnmatch(relative_path, glob):
                return cls
        return self.default_protection_class

    def _mirror_to(self, output_dir: Path) -> int:
        linked = 0
        for src_root, _dirs, files in os.walk(self.source_dir):
            rel = Path(src_root).relative_to(self.source_dir)
            dst_root = output_dir / rel
            dst_root.mkdir(parents=True, exist_ok=True)
            for fname in files:
                src = Path(src_root) / fname
                dst = dst_root / fname
                if dst.exists():
                    continue
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)
                linked += 1
        return linked

    def _apply_overrides(
        self, con: sqlite3.Connection, output_dir: Path, stats: RepackStats
    ) -> None:
        assert self._backup is not None
        for (domain, rel_path), ovr in self._overrides.items():
            row = con.execute(
                "SELECT fileID, file FROM Files "
                "WHERE domain=? AND relativePath=? LIMIT 1",
                (domain, rel_path),
            ).fetchone()
            if not row:
                raise RuntimeError(
                    f"override target not in Manifest.db: {domain}  {rel_path}"
                )
            file_id, file_bplist = row

            fp = utils.FilePlist(file_bplist)
            inner_key = self._backup._keybag.unwrapKeyForClass(
                fp.protection_class, fp.encryption_key
            )

            plaintext = ovr.local_path.read_bytes()
            ciphertext = _aes_encrypt_cbc(plaintext, inner_key)
            new_digest = hashlib.sha1(ciphertext).digest()

            blob_out = output_dir / file_id[:2] / file_id
            if blob_out.exists():
                blob_out.unlink()
            blob_out.parent.mkdir(parents=True, exist_ok=True)
            blob_out.write_bytes(ciphertext)

            new_bplist = _update_file_bplist(file_bplist, len(plaintext), new_digest)
            con.execute(
                "UPDATE Files SET file=? WHERE fileID=?", (new_bplist, file_id)
            )
            logger.debug(
                "override: %s//%s  size=%d  blob=%s",
                domain, rel_path, len(plaintext), file_id,
            )
            stats.overrides += 1

    def _apply_deletions(
        self, con: sqlite3.Connection, output_dir: Path, stats: RepackStats
    ) -> None:
        for (domain, rel_path), _ in self._deletions.items():
            row = con.execute(
                "SELECT fileID, flags FROM Files "
                "WHERE domain=? AND relativePath=? LIMIT 1",
                (domain, rel_path),
            ).fetchone()
            if not row:
                stats.deletions_missing += 1
                logger.debug("deletion miss: %s//%s", domain, rel_path)
                continue
            file_id, flags = row
            con.execute("DELETE FROM Files WHERE fileID=?", (file_id,))
            blob_out = output_dir / file_id[:2] / file_id
            if flags == 1 and blob_out.exists():
                blob_out.unlink()
            stats.deletions += 1
            logger.debug("deletion: %s//%s  fileID=%s", domain, rel_path, file_id)

    def verify_protection_classes(self) -> None:
        """
        Pre-flight check: verify every staged addition's resolved protection
        class exists in the unlocked keybag.

        Call this after staging but before :meth:`commit` for early, human-
        readable failure messages.  :meth:`commit` also calls this internally,
        but catching it here lets callers log the specific offending paths and
        abort gracefully rather than getting a cryptic ``KeyError`` from deep
        inside the crypto layer.

        Raises
        ------
        ValueError
            If any staged addition references a protection class whose key is
            absent from the keybag.  The error message names every offending
            ``domain//relativePath`` and the available classes.

        Side effects
        ------------
        Logs a WARNING for each addition assigned to class 1 or 2
        (NSFileProtectionComplete / NSFileProtectionCompleteUnlessOpen).
        These classes require the device to be unlocked before the file can
        be read — apps that access their files at first-boot time (before the
        user enters their passcode after restore) will silently fail.
        """
        self.unlock()
        assert self._backup is not None
        available: set[int] = set(self._backup._keybag.classKeys.keys())

        missing: list[str] = []
        for a in self._additions.values():
            pclass = self._addition_protection_class(
                a.domain, a.relative_path, a.protection_class
            )
            if pclass not in available:
                missing.append(
                    f"{a.domain}//{a.relative_path}: class {pclass} not in "
                    f"keybag (available: {sorted(available)})"
                )
            elif pclass in _BOOT_SENSITIVE_CLASSES:
                logger.warning(
                    "verify_protection_classes: %s//%s assigned class %d (%s) "
                    "— file will be inaccessible until user enters passcode "
                    "after first restore-reboot; "
                    "prefer class 3 (CompleteUntilFirstUserAuthentication) "
                    "or class 4 (None) for most additions",
                    a.domain, a.relative_path, pclass,
                    _BOOT_SENSITIVE_CLASS_NAMES.get(pclass, "unknown"),
                )

        if missing:
            raise ValueError(
                "IOSBackupRepacker: staged additions reference protection "
                "class(es) not present in this backup's keybag. "
                "The device would fail to unwrap these file keys after restore:\n"
                + "\n".join(f"  • {m}" for m in missing)
            )

    def _apply_additions(
        self, con: sqlite3.Connection, output_dir: Path, stats: RepackStats
    ) -> None:
        if not self._additions:
            return
        assert self._backup is not None

        # Validate protection classes before spending time on crypto work.
        # Raises ValueError with a clear per-file message if any class key is
        # absent; also emits WARNING for boot-sensitive class 1/2 assignments.
        self.verify_protection_classes()

        additions = list(self._additions.values())

        # Resolve per-addition protection classes and cache KEKs.
        classes = [
            self._addition_protection_class(
                a.domain, a.relative_path, a.protection_class
            )
            for a in additions
        ]
        used = sorted(set(classes))
        class_keys: dict[int, bytes] = {
            c: self._backup._keybag.classKeys[c][b"KEY"] for c in used
        }

        inode_seed = int(time.time()) * 1000

        # 4a. Ensure every parent directory has a flags=2 row.
        needed_dirs: set[tuple[str, str]] = set()
        for a in additions:
            parts = a.relative_path.split("/")
            for j in range(1, len(parts)):
                needed_dirs.add((a.domain, "/".join(parts[:j])))

        dir_inode = inode_seed
        for (domain, dir_rel) in sorted(needed_dirs):
            dir_fid = _make_file_id(domain, dir_rel)
            if con.execute(
                "SELECT 1 FROM Files WHERE fileID=?", (dir_fid,)
            ).fetchone():
                continue
            dir_inode += 1
            con.execute(
                "INSERT INTO Files (fileID, domain, relativePath, flags, file) "
                "VALUES (?, ?, ?, 2, ?)",
                (
                    dir_fid, domain, dir_rel,
                    _build_new_directory_bplist(
                        relative_path=dir_rel, inode=dir_inode
                    ),
                ),
            )
            stats.directories_added += 1

        # 4b. Write each addition's encrypted blob + Files row.
        for i, a in enumerate(additions, start=1):
            file_id = _make_file_id(a.domain, a.relative_path)
            pclass = classes[i - 1]

            if con.execute(
                "SELECT 1 FROM Files WHERE fileID=?", (file_id,)
            ).fetchone():
                # Already present — treat as no-op (a prior run may have added it).
                continue

            file_key = secrets.token_bytes(32)
            wrapped = _aes_key_wrap(class_keys[pclass], file_key)
            enc_key_blob = struct.pack("<I", pclass) + wrapped

            blob_out = output_dir / file_id[:2] / file_id
            if blob_out.exists():
                blob_out.unlink()
            size, digest = _stream_encrypt_and_digest(
                a.local_path, blob_out, file_key
            )

            bplist = _build_new_file_bplist(
                relative_path=a.relative_path,
                size=size,
                digest=digest,
                encryption_key_blob=enc_key_blob,
                protection_class=pclass,
                inode=inode_seed + i,
            )
            con.execute(
                "INSERT INTO Files (fileID, domain, relativePath, flags, file) "
                "VALUES (?, ?, ?, 1, ?)",
                (file_id, a.domain, a.relative_path, bplist),
            )
            stats.additions += 1
            stats.additions_bytes += size


def _slug(domain: str, relative_path: str) -> str:
    """Filesystem-safe short identifier for scratch files."""
    raw = f"{domain}_{relative_path}".replace("/", "_").replace("\\", "_")
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in raw)[:80]
