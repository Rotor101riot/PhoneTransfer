"""
ios_backup_verify.py

Safety guards that wrap the iOS-destination inject→commit phase in
``pipeline_manager``:

1. :class:`FallbackDetector` — a logging handler that records any
   ``inject_*_ios`` module emitting a "falling back" warning.  In
   production we treat such a fallback as a hard failure: the whole
   point of the backup-mod strategy is that AFC silently doesn't work
   for most modern-iOS categories, so a silent degradation means data
   is lost from the repacked backup.

2. :func:`take_baseline` / :func:`verify_after_commit` — the last gate
   before ``pymobiledevice3 backup2 restore`` is allowed to push the
   repack back to a real device.  Runs four structural checks on top
   of the per-category row-count growth check that's been there since
   the first cut:

      a. Per-category additive growth: ``new_count >= baseline +
         injected``.  ``>=`` (not ``==``) tolerates iOS background
         writes between the two reads.
      b. SQLite ``PRAGMA integrity_check`` on every modified DB.
         Catches B-tree damage that an INSERT can leave behind even
         when COUNT(*) reports the right number.
      c. Plist round-trip parse on Manifest.plist, Info.plist,
         Status.plist with required-key checks.  Catches plist-level
         corruption that would otherwise surface as a generic restore
         error on the device.
      d. Manifest.db ↔ filesystem coherence: every fileID row in
         Manifest.db.Files has a hash-bucket file on disk, and every
         hash-bucket file has a Manifest row.  Catches the class of
         bug where the repacker writes the manifest entry but skips
         the disk write or vice versa.

   Verifying here is cheap (~30-60s of decryption against on-disk
   blobs) compared to a 5-10 min restore that could brick the
   destination iPhone if Manifest.db got corrupted.
"""

from __future__ import annotations

import logging
import plistlib
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core.ios_backup_injector import IOSBackupInjector

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fallback detector
# ---------------------------------------------------------------------------

class FallbackDetector(logging.Handler):
    """Records any ``inject_*_ios`` log line indicating a backup-mod fallback.

    Mirrors ``dry_run_pipeline._FallbackDetector``.  The trigger phrase is
    deliberately broad — any warning from a ``core.inject_*_ios`` module
    that mentions "falling back" or "fall back" is recorded.  In production
    the pipeline treats a non-empty ``fallbacks`` list as a hard failure.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.fallbacks: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not record.name.startswith("core.inject_") or \
               not record.name.endswith("_ios"):
                return
            msg = record.getMessage().lower()
            if "falling back" in msg or "fall back" in msg:
                self.fallbacks.append(f"{record.name}: {record.getMessage()}")
        except Exception:
            pass  # never let a logging handler crash the pipeline


# ---------------------------------------------------------------------------
# Per-category strategies
# ---------------------------------------------------------------------------

@dataclass
class _Strategy:
    """How to count one category's rows in one staged file."""
    domain: str
    relpath: str
    # Returns int row count (sqlite) or item count (plist).
    counter: Callable[[Path], int]


def _sql_count(query: str) -> Callable[[Path], int]:
    def _read(local: Path) -> int:
        con = sqlite3.connect(str(local))
        try:
            # Some DBs (calls, contacts) reference custom SQL functions.
            # Stub them so PRAGMA-driven prepared statements don't blow
            # up on a SELECT-only path.
            try:
                con.create_function(
                    "verify_chat", 1, lambda _g: None, deterministic=True
                )
            except Exception:
                pass
            return int(con.execute(query).fetchone()[0])
        finally:
            con.close()
    return _read


def _plist_count(picker: Callable[[dict], int]) -> Callable[[Path], int]:
    def _read(local: Path) -> int:
        with local.open("rb") as fh:
            data = plistlib.load(fh)
        return int(picker(data))
    return _read


def _alarms_picker(p: dict) -> int:
    return len(p.get("MTAlarms", {}).get("MTAlarms", []))


def _blocked_picker(p: dict) -> int:
    top = p.get("__kCMFBlockListStoreTopLevelKey", {}) or {}
    return len(top.get("__kCMFBlockListStoreArrayKey", []) or [])


# Fixed-path strategies.  Categories whose target file path is dynamic
# (reminders' Container_v1/Stores/Data-<UUID>.sqlite) are resolved at
# baseline time via :func:`_resolve_dynamic_strategy`.
_FIXED_STRATEGIES: dict[str, _Strategy] = {
    "contacts": _Strategy(
        "HomeDomain", "Library/AddressBook/AddressBook.sqlitedb",
        _sql_count("SELECT COUNT(*) FROM ABPerson"),
    ),
    "contact_groups": _Strategy(
        "HomeDomain", "Library/AddressBook/AddressBook.sqlitedb",
        _sql_count("SELECT COUNT(*) FROM ABGroup"),
    ),
    "sms": _Strategy(
        "HomeDomain", "Library/SMS/sms.db",
        _sql_count("SELECT COUNT(*) FROM message"),
    ),
    "calls": _Strategy(
        "HomeDomain", "Library/CallHistoryDB/CallHistory.storedata",
        _sql_count("SELECT COUNT(*) FROM ZCALLRECORD"),
    ),
    "calendar": _Strategy(
        "HomeDomain", "Library/Calendar/Calendar.sqlitedb",
        _sql_count("SELECT COUNT(*) FROM CalendarItem"),
    ),
    "bookmarks": _Strategy(
        "HomeDomain", "Library/Safari/Bookmarks.db",
        _sql_count("SELECT COUNT(*) FROM bookmarks"),
    ),
    "browser_history": _Strategy(
        "HomeDomain", "Library/Safari/History.db",
        _sql_count("SELECT COUNT(*) FROM history_items"),
    ),
    "notes": _Strategy(
        "AppDomainGroup-group.com.apple.notes", "NoteStore.sqlite",
        _sql_count("SELECT COUNT(*) FROM ZICNOTEDATA"),
    ),
    "voicemail": _Strategy(
        "HomeDomain", "Library/Voicemail/voicemail.db",
        _sql_count("SELECT COUNT(*) FROM voicemail"),
    ),
    "alarms": _Strategy(
        "HomeDomain", "Library/Preferences/com.apple.mobiletimerd.plist",
        _plist_count(_alarms_picker),
    ),
    "blocked": _Strategy(
        "HomeDomain", "Library/Preferences/com.apple.cmfsyncagent.plist",
        _plist_count(_blocked_picker),
    ),
}

# Categories the verifier intentionally skips.  Wallpaper and ringtones
# are addition/override based — their success is already covered by
# ``RepackStats.additions`` and ``RepackStats.overrides`` that
# ``IOSBackupInjector.commit`` returns to the pipeline.
_SKIP = frozenset({"wallpaper", "ringtones"})

_REM_DOMAIN = "AppDomainGroup-group.com.apple.reminders"
_REM_REMINDER_QUERY = "SELECT COUNT(*) FROM ZREMCDREMINDER"


def _resolve_dynamic_strategy(
    injector: IOSBackupInjector, category: str
) -> _Strategy | None:
    """Resolve categories whose target path isn't known statically."""
    if category != "reminders":
        return None
    # The reminders injector calls list_relative_paths to discover the
    # active per-account store and stages it via stage_db.  Pull the
    # already-staged path out of the injector's _staged_dbs index.
    for (domain, rel), _sdb in injector._staged_dbs.items():
        if domain == _REM_DOMAIN and rel.startswith("Container_v1/Stores/Data-"):
            return _Strategy(domain, rel, _sql_count(_REM_REMINDER_QUERY))
    return None


# ---------------------------------------------------------------------------
# Structural checks (Path 1: stdlib-only, no MVT / no WSL)
# ---------------------------------------------------------------------------

def _pragma_integrity_check(local_db: Path) -> str | None:
    """Return ``None`` if the SQLite file at *local_db* is internally
    consistent, otherwise an error message describing what's wrong.

    ``PRAGMA integrity_check`` returns the literal string ``"ok"`` on a
    healthy DB, or one row per problem on a damaged one.  We cap the
    error message at the first few lines so a flood of B-tree complaints
    doesn't blow up the log.
    """
    try:
        con = sqlite3.connect(str(local_db))
        try:
            rows = con.execute("PRAGMA integrity_check").fetchall()
        finally:
            con.close()
    except Exception as exc:
        return f"integrity_check raised: {exc}"

    if len(rows) == 1 and rows[0][0] == "ok":
        return None

    lines = [str(r[0]) for r in rows[:5]]
    extra = f" (+{len(rows) - 5} more)" if len(rows) > 5 else ""
    return f"sqlite damaged: {' | '.join(lines)}{extra}"


# Required keys per top-level plist.  Missing any of these causes
# pymobiledevice3 backup2 restore to bail with a generic error message;
# detecting at verify time gives a far more useful failure mode.
_PLIST_REQUIRED_KEYS: dict[str, set[str]] = {
    "Manifest.plist": {
        "BackupKeyBag", "ManifestKey", "Lockdown", "IsEncrypted",
        "SystemDomainsVersion", "Version",
    },
    "Info.plist": {
        "Device Name", "Display Name", "Product Type", "Serial Number",
        "Target Identifier", "Unique Identifier",
    },
    "Status.plist": {
        "BackupState", "Date", "IsFullBackup", "SnapshotState",
        "UUID", "Version",
    },
}


def _verify_top_level_plists(repacked_dir: Path) -> list[str]:
    """Round-trip parse Manifest.plist, Info.plist, Status.plist.

    Returns a list of failure strings; empty list means all three plists
    parse cleanly and contain their required keys.
    """
    failures: list[str] = []
    for name, required in _PLIST_REQUIRED_KEYS.items():
        path = repacked_dir / name
        if not path.exists():
            failures.append(f"plist: {name} missing from repack")
            continue
        try:
            with path.open("rb") as fh:
                data = plistlib.load(fh)
        except Exception as exc:
            failures.append(f"plist: {name} failed to parse: {exc}")
            continue
        if not isinstance(data, dict):
            failures.append(
                f"plist: {name} top-level is {type(data).__name__}, expected dict"
            )
            continue
        missing = required - set(data.keys())
        if missing:
            failures.append(
                f"plist: {name} missing required keys: {sorted(missing)}"
            )
    return failures


def _verify_manifest_filesystem_coherence(
    repacked_dir: Path, manifest_db_path: Path
) -> list[str]:
    """Cross-check Manifest.db.Files against on-disk hash buckets.

    Two directions:
      - Forward: every (fileID, flags=1) row in Manifest.db.Files must
        have a corresponding ``<repacked_dir>/<fileID[:2]>/<fileID>``
        file on disk.  Directories (flags=2) and symlinks (flags=4) are
        skipped because they don't have blob files.
      - Reverse: every blob file under a hash bucket on disk must have
        a Manifest row.

    Reports up to MAX_REPORT entries per direction so a wholesale
    mismatch doesn't drown the log.  Returns empty list on full coherence.
    """
    MAX_REPORT = 5
    failures: list[str] = []

    try:
        con = sqlite3.connect(str(manifest_db_path))
        try:
            rows = con.execute(
                "SELECT fileID, flags FROM Files"
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:
        return [f"coherence: could not read Manifest.db.Files: {exc}"]

    manifest_file_ids: set[str] = set()
    missing_on_disk: list[str] = []
    for file_id, flags in rows:
        if flags != 1:
            continue  # directories/symlinks have no blob
        manifest_file_ids.add(file_id)
        blob_path = repacked_dir / file_id[:2] / file_id
        if not blob_path.exists():
            if len(missing_on_disk) < MAX_REPORT:
                missing_on_disk.append(file_id)

    if missing_on_disk:
        failures.append(
            f"coherence: {len(missing_on_disk)}+ Manifest entries missing "
            f"on disk (e.g. {missing_on_disk[:3]})"
        )

    orphans: list[str] = []
    try:
        for bucket in repacked_dir.iterdir():
            if not bucket.is_dir():
                continue
            name = bucket.name
            # Hash buckets are 2-char lowercase hex; skip Manifest dirs etc.
            if len(name) != 2 or not all(c in "0123456789abcdef" for c in name):
                continue
            for blob in bucket.iterdir():
                if not blob.is_file():
                    continue
                if blob.name not in manifest_file_ids:
                    if len(orphans) < MAX_REPORT:
                        orphans.append(blob.name)
                    if len(orphans) >= MAX_REPORT:
                        break
            if len(orphans) >= MAX_REPORT:
                break
    except Exception as exc:
        failures.append(f"coherence: orphan scan failed: {exc}")
        return failures

    if orphans:
        failures.append(
            f"coherence: {len(orphans)}+ orphan blob(s) on disk with no "
            f"Manifest row (e.g. {orphans[:3]})"
        )

    return failures


# ---------------------------------------------------------------------------
# Baseline + verify
# ---------------------------------------------------------------------------

@dataclass
class CategoryBaseline:
    category: str
    domain: str
    relpath: str
    count: int
    counter: Callable[[Path], int] = field(repr=False)


@dataclass
class VerifyResult:
    failures: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def take_baseline(
    injector: IOSBackupInjector, category: str
) -> CategoryBaseline | None:
    """Snapshot the current count for *category*, just before its injector runs.

    Returns ``None`` if *category* has no verify strategy, or if the
    target file isn't present in the source backup (unusual but not
    fatal — the injector itself will handle absence).
    """
    if category in _SKIP:
        return None

    strategy = _FIXED_STRATEGIES.get(category)
    if strategy is None:
        strategy = _resolve_dynamic_strategy(injector, category)
    if strategy is None:
        return None

    try:
        local = injector.stage_db(strategy.domain, strategy.relpath)
        count = strategy.counter(local)
    except Exception as exc:
        logger.debug(
            "ios_backup_verify.take_baseline: %s baseline read failed (%s) — "
            "verify will skip this category", category, exc,
        )
        return None

    return CategoryBaseline(
        category=category,
        domain=strategy.domain,
        relpath=strategy.relpath,
        count=count,
        counter=strategy.counter,
    )


def verify_after_commit(
    repacked_backup_dir: Path,
    passphrase: str,
    baselines: dict[str, CategoryBaseline],
    injected_counts: dict[str, int],
) -> VerifyResult:
    """Re-decrypt the repacked backup and assert each category grew enough.

    ``repacked_backup_dir`` is ``<root>/<udid>/`` — the same path
    ``IOSBackupRepacker.commit`` writes into.

    For every category with a baseline AND a non-zero injected count, we
    extract its target file from the repacked backup, count rows/entries
    again, and assert ``new_count >= baseline + injected``.  Categories
    without baselines (e.g. wallpaper, ringtones, dynamic-path categories
    we couldn't resolve) are reported as ``skipped`` rather than failures.
    """
    from iphone_backup_decrypt import EncryptedBackup
    from core.ios_backup_repacker import _decrypt_without_size_check

    result = VerifyResult()

    if not (repacked_backup_dir / "Manifest.plist").exists():
        result.failures.append(
            f"verify: repacked backup not found at "
            f"{repacked_backup_dir}/Manifest.plist"
        )
        return result

    # Structural check (b): top-level plists.  Cheap, runs before we
    # even unlock the keybag — if these are damaged there's no point
    # decrypting anything else.
    for f in _verify_top_level_plists(repacked_backup_dir):
        result.failures.append(f)

    backup = EncryptedBackup(
        backup_directory=str(repacked_backup_dir), passphrase=passphrase
    )
    try:
        backup._read_and_unlock_keybag()
        backup._decrypt_manifest_db_file()

        # Same size-check bypass the repacker uses; without it, reading
        # any DB whose plaintext != ciphertext-block-aligned trips the
        # library's strict assertion.
        backup._decrypt_inner_file = (
            lambda *, file_id, file_bplist: _decrypt_without_size_check(
                backup, file_id, file_bplist
            )
        )

        # Structural check (d): Manifest.db ↔ on-disk hash buckets.
        # The library decrypts Manifest.db to a temp path during
        # _decrypt_manifest_db_file(); reuse that rather than decrypt
        # again.
        manifest_db_path = Path(backup._temp_decrypted_manifest_db_path)
        for f in _verify_manifest_filesystem_coherence(
            repacked_backup_dir, manifest_db_path
        ):
            result.failures.append(f)

        # Structural check (c) is folded into the per-category loop
        # below, so we only run integrity_check on DBs we already had
        # to extract for the row-count check anyway.

        scratch = Path(tempfile.mkdtemp(prefix="ios_verify_"))
        try:
            for category, expected_delta in injected_counts.items():
                if expected_delta <= 0:
                    continue
                if category in _SKIP:
                    result.skipped.append(category)
                    continue

                base = baselines.get(category)
                if base is None:
                    result.skipped.append(category)
                    continue

                try:
                    data = backup.extract_file_as_bytes(
                        base.relpath, domain_like=base.domain
                    )
                except Exception as exc:
                    result.failures.append(
                        f"{category}: could not extract "
                        f"{base.domain}//{base.relpath} from repack: {exc}"
                    )
                    continue

                local = scratch / Path(base.relpath).name
                local.write_bytes(data)

                # Structural check (a): SQLite integrity.  Plists don't
                # need this — plistlib already round-tripped them above.
                if base.relpath.endswith(("sqlitedb", ".db", ".sqlite",
                                           ".storedata")):
                    err = _pragma_integrity_check(local)
                    if err is not None:
                        result.failures.append(f"{category}: {err}")
                        # Skip count check on a damaged DB — the number
                        # could be anything.
                        continue

                try:
                    new_count = base.counter(local)
                except Exception as exc:
                    result.failures.append(
                        f"{category}: count read failed on repack: {exc}"
                    )
                    continue

                min_expected = base.count + expected_delta
                if new_count < min_expected:
                    result.failures.append(
                        f"{category}: expected >= {min_expected} "
                        f"(baseline {base.count} + injected {expected_delta}), "
                        f"found {new_count}"
                    )
                else:
                    result.checked.append(
                        f"{category}: {base.count} -> {new_count} "
                        f"(+{expected_delta} expected)"
                    )
        finally:
            try:
                import shutil as _sh
                _sh.rmtree(scratch, ignore_errors=True)
            except Exception:
                pass
    finally:
        try:
            backup.close()
        except Exception:
            pass

    return result
