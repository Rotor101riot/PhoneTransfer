from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.normalization_schema import Bookmark

logger = logging.getLogger(__name__)

_APPLE_EPOCH_OFFSET = 978307200.0  # seconds between 1970-01-01 and 2001-01-01
_BACKUP_DOMAIN = "HomeDomain"
_BACKUP_REL_PATH = "Library/Safari/Bookmarks.db"
_AFC2_PATH = "/var/mobile/Library/Safari/Bookmarks.db"


def _apple_ts_to_datetime(ts: float | None) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts) + _APPLE_EPOCH_OFFSET, tz=timezone.utc)
    except Exception:
        return None


def _build_folder_map(cur: sqlite3.Cursor) -> dict[int, str]:
    """Return a mapping of {id: title} for all folder rows (type=1)."""
    try:
        cur.execute(
            "SELECT id, title FROM bookmarks WHERE type=1"
        )
        return {row[0]: (row[1] or "") for row in cur.fetchall()}
    except Exception as exc:
        logger.debug("Could not build folder map: %s", exc)
        return {}


def _resolve_folder(
    parent_id: int | None,
    folder_map: dict[int, str],
) -> str | None:
    """Walk up the folder map to resolve the folder name for a bookmark."""
    if parent_id is None:
        return None
    name = folder_map.get(parent_id)
    return name if name else None


def _parse_bookmarks_db(db_path: Path) -> list[Bookmark]:
    """Parse a Safari Bookmarks.db SQLite file and return Bookmark objects."""
    bookmarks: list[Bookmark] = []
    try:
        with sqlite3.connect(str(db_path)) as con:
            cur = con.cursor()

            folder_map = _build_folder_map(cur)

            # Detect the date column name — older Safari schemas use
            # "recordChangeDate", newer use "last_modified" or "added".
            bm_cols = {r[1] for r in cur.execute("PRAGMA table_info(bookmarks)").fetchall()}
            if "recordChangeDate" in bm_cols:
                date_col = "recordChangeDate"
            elif "last_modified" in bm_cols:
                date_col = "last_modified"
            elif "added" in bm_cols:
                date_col = "added"
            else:
                date_col = None

            select_cols = f"title, url, parent, {date_col}" if date_col else "title, url, parent, NULL"
            cur.execute(
                f"SELECT {select_cols} "
                "FROM bookmarks "
                "WHERE type=0 AND url IS NOT NULL AND url != ''"
            )
            for row in cur.fetchall():
                try:
                    title, url, parent, change_date = row
                    added = _apple_ts_to_datetime(change_date)
                    folder = _resolve_folder(parent, folder_map)
                    bookmarks.append(
                        Bookmark(
                            title=title or url,
                            url=url,
                            folder=folder,
                            added=added,
                        )
                    )
                except Exception as exc:
                    logger.warning("Skipping malformed bookmark row %r: %s", row, exc)

        logger.info("Parsed %d bookmark(s) from %s", len(bookmarks), db_path)
    except Exception as exc:
        logger.error("Failed to parse Safari Bookmarks.db at %s: %s", db_path, exc)
    return bookmarks


def _try_iOSbackup(device_id: str, staging_dir: Path) -> list[Bookmark]:
    try:
        from core.device_connection_cache import get_iosbackup

        backup = get_iosbackup(device_id)
        staging_dir.mkdir(parents=True, exist_ok=True)
        tmp_name = Path(_BACKUP_REL_PATH).name
        info = backup.getFileDecryptedCopy(
            relativePath=_BACKUP_REL_PATH,
            targetName=tmp_name,
            targetFolder=str(staging_dir),
        )
        if not info:
            logger.warning("Unexpected iOSbackup return for bookmarks on %s", device_id)
            return []
        db_path = staging_dir / tmp_name
        return _parse_bookmarks_db(db_path)
    except Exception as exc:
        logger.error(
            "iOSbackup bookmark extraction failed for device %s: %s", device_id, exc
        )
        return []


def _try_afc2(device_id: str, staging_dir: Path) -> list[Bookmark]:
    try:
        from core.device_connection_cache import get_broker
        from core.afc2_connector import AFC2Connector

        broker = get_broker(device_id)
        afc2 = AFC2Connector(broker)
        db_bytes = afc2.read_file(_AFC2_PATH)

        if db_bytes is None:
            logger.warning("AFC2 could not read %s for device %s", _AFC2_PATH, device_id)
            return []

        local_db = staging_dir / "SafariBookmarks_afc2.db"
        local_db.write_bytes(db_bytes)
        logger.info(
            "Pulled Safari Bookmarks.db via AFC2 (%d bytes) to %s",
            len(db_bytes),
            local_db,
        )
        return _parse_bookmarks_db(local_db)
    except Exception as exc:
        logger.error(
            "AFC2 bookmark extraction failed for device %s: %s", device_id, exc
        )
        return []


def extract(device_id: str, staging_dir: Path, is_privileged: bool) -> list[Bookmark]:
    """Extract Safari bookmarks from an iOS device.

    Tries iOSbackup first, then AFC2 for jailbroken devices.

    Args:
        device_id: iOS UDID.
        staging_dir: Local directory for temporary files.
        is_privileged: True if the device is jailbroken.

    Returns:
        A list of Bookmark objects, or [] on failure.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)

    bookmarks = _try_iOSbackup(device_id, staging_dir)
    if bookmarks:
        return bookmarks

    if is_privileged:
        bookmarks = _try_afc2(device_id, staging_dir)
        if bookmarks:
            return bookmarks

    logger.error(
        "All Safari bookmark extraction methods failed for device %s.", device_id
    )
    return []
