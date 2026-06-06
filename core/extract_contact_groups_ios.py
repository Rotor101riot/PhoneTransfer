"""
extract_contact_groups_ios.py

Extracts contact groups from an iOS device.

Strategy
--------
Groups live in the same `AddressBook.sqlitedb` as contacts (HomeDomain,
Library/AddressBook/AddressBook.sqlitedb), in the `ABGroup` and
`ABGroupMembers` tables.  There is no dedicated public service for groups
(the AddressBook vCard exporter ignores ABGroup), so the only path is
pulling the SQLite DB directly:

1. AFC2 (jailbroken) — pull the live DB.
2. iOSbackup (non-jailbroken) — pull from the encrypted backup copy.

Member resolution: each ABGroupMembers row references an ABPerson by
ROWID; we join back to ABPerson.First/Last and emit "First Last" strings
in `ContactGroup.member_names` so `inject_contact_groups_ios` can
re-resolve them after the contacts injector has populated the
destination's ABPerson rows.

Never raises — all exceptions are caught, logged, and result in an empty
return value.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from core.normalization_schema import ContactGroup

logger = logging.getLogger(__name__)

_AB_DOMAIN = "HomeDomain"
_AB_RELATIVE_PATH = "Library/AddressBook/AddressBook.sqlitedb"
_AB_DEVICE_PATH = "/var/mobile/Library/AddressBook/AddressBook.sqlitedb"

# member_type=0 in ABGroupMembers means "ABPerson reference"
_MEMBER_TYPE_PERSON = 0


def extract(udid: str, staging_dir: Path, is_jailbroken: bool = False) -> list[ContactGroup]:
    """
    Extract all contact groups from the iOS device identified by *udid*.

    Returns
    -------
    list[ContactGroup]   Possibly empty on total failure.
    """
    try:
        return _extract_impl(udid, staging_dir, is_jailbroken)
    except Exception as exc:
        logger.exception(
            "extract_contact_groups_ios: top-level failure for %s: %s", udid, exc
        )
        return []


def _extract_impl(udid: str, staging_dir: Path, is_jailbroken: bool) -> list[ContactGroup]:
    work_dir = staging_dir / "contact_groups_ios"
    work_dir.mkdir(parents=True, exist_ok=True)

    db_path = _pull_addressbook_db(udid, work_dir, is_jailbroken)
    if db_path is None:
        logger.warning(
            "contact_groups_ios: could not obtain AddressBook.sqlitedb for %s", udid
        )
        return []

    groups = _parse_groups(db_path)
    logger.info(
        "contact_groups_ios: extracted %d group(s) for %s", len(groups), udid
    )
    return groups


def _pull_addressbook_db(udid: str, work_dir: Path, is_jailbroken: bool) -> Path | None:
    """
    Pull AddressBook.sqlitedb to the local filesystem.  Reuses the same
    helper as the contacts extractor so both modules behave identically
    against the same source DB.
    """
    local_db = work_dir / "AddressBook.sqlitedb"

    if is_jailbroken:
        try:
            from core.device_connection_cache import get_broker
            from core.afc2_connector import AFC2Connector

            broker = get_broker(udid)
            with AFC2Connector(broker) as afc2:
                ok = afc2.pull_file(_AB_DEVICE_PATH, local_db)
            if ok and local_db.exists():
                logger.debug("contact_groups_ios: pulled DB via AFC2")
                return local_db
        except PermissionError:
            logger.warning(
                "contact_groups_ios: AFC2 not available despite is_jailbroken=True"
            )
        except Exception as exc:
            logger.warning("contact_groups_ios: AFC2 pull failed: %s", exc)

    try:
        from core.device_connection_cache import get_iosbackup
        local_db.parent.mkdir(parents=True, exist_ok=True)
        backup = get_iosbackup(udid)
        info = backup.getFileDecryptedCopy(
            relativePath=_AB_RELATIVE_PATH,
            targetName=local_db.name,
            targetFolder=str(local_db.parent),
        )
        if info and local_db.exists():
            logger.debug("contact_groups_ios: pulled DB via iOSbackup")
            return local_db
    except Exception as exc:
        logger.warning("contact_groups_ios: iOSbackup pull failed: %s", exc)

    return None


def _parse_groups(db_path: Path) -> list[ContactGroup]:
    """
    Read ABGroup + ABGroupMembers + ABPerson and produce ContactGroup
    objects with `member_names` filled as "First Last" strings.
    """
    groups: list[ContactGroup] = []

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row

            # ABGroup may not exist on a freshly-restored / empty DB —
            # treat that as zero groups rather than an error.
            try:
                rows = conn.execute(
                    "SELECT ROWID, Name FROM ABGroup ORDER BY ROWID"
                ).fetchall()
            except sqlite3.OperationalError as exc:
                logger.debug("contact_groups_ios: ABGroup query failed: %s", exc)
                return []

            members_by_group = _load_members(conn)

            for row in rows:
                title = (row["Name"] or "").strip()
                if not title:
                    continue
                names = members_by_group.get(int(row["ROWID"]), [])
                groups.append(
                    ContactGroup(
                        title=title,
                        group_id=int(row["ROWID"]),
                        visible=True,
                        member_count=len(names),
                        member_names=names,
                    )
                )

    except Exception as exc:
        logger.exception("contact_groups_ios: failed to parse %s: %s", db_path, exc)

    return groups


def _load_members(conn: sqlite3.Connection) -> dict[int, list[str]]:
    """
    Return {group_rowid: ["First Last", ...]} for every ABPerson member.
    Members with neither First nor Last are skipped — they can't be
    resolved by the injector's name-based lookup anyway.
    """
    out: dict[int, list[str]] = {}
    try:
        cur = conn.execute(
            """
            SELECT m.group_id, p.First, p.Last
            FROM ABGroupMembers m
            JOIN ABPerson p ON p.ROWID = m.member_id
            WHERE m.member_type = ?
            ORDER BY m.group_id, m.ROWID
            """,
            (_MEMBER_TYPE_PERSON,),
        )
        for row in cur.fetchall():
            first = (row["First"] or "").strip()
            last = (row["Last"] or "").strip()
            if not first and not last:
                continue
            full = f"{first} {last}".strip()
            out.setdefault(int(row["group_id"]), []).append(full)
    except sqlite3.OperationalError as exc:
        logger.debug("contact_groups_ios: ABGroupMembers query failed: %s", exc)
    return out
