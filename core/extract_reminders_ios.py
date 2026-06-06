from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.normalization_schema import Reminder

logger = logging.getLogger(__name__)

# Apple CoreData epoch starts 2001-01-01; Unix epoch starts 1970-01-01.
_APPLE_EPOCH_OFFSET = 978307200.0  # seconds


def _apple_ts_to_datetime(ts: float | None) -> datetime | None:
    """Convert an Apple CoreData timestamp (seconds since 2001-01-01) to datetime."""
    if ts is None:
        return None
    try:
        unix_ts = float(ts) + _APPLE_EPOCH_OFFSET
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    except Exception:
        return None


def _discover_columns(cursor: sqlite3.Cursor, table: str) -> set[str]:
    """Return the set of column names for a given table."""
    try:
        cursor.execute(f"PRAGMA table_info({table})")
        return {row[1].upper() for row in cursor.fetchall()}
    except Exception:
        return set()


def _parse_reminders_db(db_path: Path) -> list[Reminder]:
    """Parse a Reminders SQLite database and return Reminder objects."""
    reminders: list[Reminder] = []
    try:
        with sqlite3.connect(str(db_path)) as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()

            # Discover available tables
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0].upper() for row in cur.fetchall()}
            logger.debug("Tables in Reminders DB: %s", tables)

            # Try primary table (iOS 13+)
            target_table: str | None = None
            for candidate in ("ZREMCDREMINDER", "CREMINDER", "REMINDER"):
                if candidate in tables:
                    target_table = candidate
                    break

            if target_table is None:
                logger.warning("No known reminder table found; tables: %s", tables)
                return []

            cols = _discover_columns(cur, target_table)

            # Map known column name variants
            title_col = next((c for c in ("ZTITLE", "ZSUMMARY", "TITLE") if c in cols), None)
            notes_col = next((c for c in ("ZBODY", "ZNOTES", "NOTES", "ZDESCRIPTION") if c in cols), None)
            due_col = next((c for c in ("ZDUEDATE", "DUEDATE", "ZDUEDATE2") if c in cols), None)
            completed_col = next((c for c in ("ZCOMPLETED", "ZISCOMPLETED", "COMPLETED") if c in cols), None)
            priority_col = next((c for c in ("ZPRIORITY", "PRIORITY") if c in cols), None)
            uid_col = next((c for c in ("ZEXTERNALIDENTIFIER", "UUID", "ZUID") if c in cols), None)

            if title_col is None:
                logger.warning(
                    "Cannot identify title column in table %s; cols: %s",
                    target_table,
                    cols,
                )
                return []

            select_cols = [title_col]
            for c in (notes_col, due_col, completed_col, priority_col, uid_col):
                if c:
                    select_cols.append(c)

            query = f"SELECT {', '.join(select_cols)} FROM {target_table}"  # noqa: S608
            cur.execute(query)

            if cur.description is None:
                logger.warning("Reminders query returned no description for table %s", target_table)
                return []

            col_names = [desc[0].upper() for desc in cur.description]

            def get(row: sqlite3.Row, col: str | None) -> object:
                if col is None:
                    return None
                try:
                    idx = col_names.index(col.upper())
                    return row[idx]
                except (ValueError, IndexError):
                    return None

            for row in cur.fetchall():
                try:
                    title = str(get(row, title_col) or "")
                    notes_val = get(row, notes_col)
                    notes = str(notes_val) if notes_val is not None else None
                    due_raw = get(row, due_col)
                    due_dt = _apple_ts_to_datetime(due_raw) if due_raw is not None else None
                    completed_raw = get(row, completed_col)
                    completed = bool(completed_raw) if completed_raw is not None else False
                    priority_raw = get(row, priority_col)
                    priority = int(priority_raw) if priority_raw is not None else 0
                    uid_raw = get(row, uid_col)
                    uid = str(uid_raw) if uid_raw is not None else None
                    reminders.append(
                        Reminder(
                            title=title,
                            due=due_dt,
                            notes=notes,
                            completed=completed,
                            list_name=None,
                            uid=uid,
                            priority=priority,
                        )
                    )
                except Exception as exc:
                    logger.warning("Skipping malformed reminder row: %s", exc)

        logger.info("Parsed %d reminder(s) from %s", len(reminders), db_path)
    except Exception as exc:
        logger.error("Failed to parse Reminders DB at %s: %s", db_path, exc)
    return reminders


def _find_reminders_paths(backup_dir: Path) -> list[tuple[str, str]]:
    """
    Query Manifest.db for Reminders SQLite files.

    Modern iOS (13+) stores reminders in the AppDomainGroup under
    Container_v1/Stores/Data-{UUID}.sqlite.  Older iOS used HomeDomain.
    Returns a list of (domain, relativePath) tuples to try.
    """
    results: list[tuple[str, str]] = []
    manifest_db = backup_dir / "Manifest.db"
    if manifest_db.exists():
        try:
            conn = sqlite3.connect(str(manifest_db))
            rows = conn.execute(
                "SELECT domain, relativePath FROM Files "
                "WHERE domain = 'AppDomainGroup-group.com.apple.reminders' "
                "AND relativePath LIKE 'Container_v1/Stores/Data-%.sqlite'"
            ).fetchall()
            conn.close()
            results.extend((r[0], r[1]) for r in rows if r[1])
        except Exception as exc:
            logger.debug("reminders: Manifest.db query failed: %s", exc)
    # Legacy path fallback
    results.append(("HomeDomain", "Library/Reminders/RemindersDB"))
    return results


def _try_iOSbackup(device_id: str, staging_dir: Path) -> list[Reminder]:
    """Try to extract the Reminders DB via iOSbackup (non-jailbroken)."""
    try:
        from core.device_connection_cache import get_iosbackup
        from core.backup_manager_ios import BackupManager

        backup = get_iosbackup(device_id)

        # Determine the backup directory so we can query its Manifest.db
        try:
            mgr = BackupManager(udid=device_id)
            candidates = _find_reminders_paths(mgr.backup_dir)
        except Exception:
            candidates = [("HomeDomain", "Library/Reminders/RemindersDB")]

        all_reminders: list[Reminder] = []
        # iOSbackup searches all domains by relativePath — no domain kwarg needed.
        for domain, rel_path in candidates:
            try:
                tmp_name = Path(rel_path).name
                tmp_path = staging_dir / tmp_name
                if tmp_path.exists():
                    tmp_path.unlink()
                tmp_path.parent.mkdir(parents=True, exist_ok=True)
                info = backup.getFileDecryptedCopy(
                    relativePath=rel_path,
                    targetName=tmp_name,
                    targetFolder=str(tmp_path.parent),
                )
                if not info or not tmp_path.exists():
                    continue
                result = _parse_reminders_db(tmp_path)
                logger.info(
                    "reminders: pulled %s/%s → %d items", domain, rel_path, len(result)
                )
                all_reminders.extend(result)
            except Exception as exc:
                logger.debug(
                    "iOSbackup: could not retrieve %s/%s: %s", domain, rel_path, exc
                )
        return all_reminders
    except Exception as exc:
        logger.error("iOSbackup initialization failed for device %s: %s", device_id, exc)
    return []


def _try_afc2(device_id: str, staging_dir: Path) -> list[Reminder]:
    """Try to extract the Reminders DB via AFC2 (jailbroken)."""
    try:
        from pymobiledevice3.services.afc import AfcService
        from core.device_connection_cache import get_lockdown

        lockdown = get_lockdown(device_id)
        afc2 = AfcService(lockdown=lockdown, service_name="com.apple.afc2")

        device_db_path = "/var/mobile/Library/Reminders/RemindersDB"
        db_bytes = afc2.get_file_contents(device_db_path)

        local_db = staging_dir / "RemindersDB_afc2.db"
        local_db.write_bytes(db_bytes)
        logger.info(
            "Pulled Reminders DB via AFC2 (%d bytes) to %s", len(db_bytes), local_db
        )
        return _parse_reminders_db(local_db)
    except Exception as exc:
        logger.error("AFC2 Reminders extraction failed for device %s: %s", device_id, exc)
        return []


def extract(device_id: str, staging_dir: Path, is_privileged: bool) -> list[Reminder]:
    """Extract reminders from an iOS device.

    Tries iOSbackup first (works for both jailbroken and non-jailbroken if a
    backup exists), then falls back to AFC2 for jailbroken devices.

    Args:
        device_id: iOS UDID.
        staging_dir: Local directory for temporary files.
        is_privileged: True if the device is jailbroken.

    Returns:
        A list of Reminder objects, or [] on failure.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)

    reminders = _try_iOSbackup(device_id, staging_dir)
    if reminders:
        return reminders

    if is_privileged:
        reminders = _try_afc2(device_id, staging_dir)
        if reminders:
            return reminders

    logger.error(
        "All Reminders extraction methods failed for device %s.", device_id
    )
    return []
