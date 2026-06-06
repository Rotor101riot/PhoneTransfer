"""
inject_contact_groups_ios.py

Inject contact groups into iOS Contacts.

Backup-mod path only: groups live in the same `AddressBook.sqlitedb` as
contacts, in the `ABGroup` (one row per group) and `ABGroupMembers` (one
row per member) tables.  Pre-iOS-9 these were writable through standard
AFC; modern iOS protects the AddressBook domain, so the only realistic
non-jailbreak path is via backup-mod.

When no `IOSBackupInjector` session is active, this module logs guidance
and returns 0 — there is no AFC fallback because the AFC fallback for
contacts pushes vCards (`.vcf` files) which iOS doesn't extend to group
membership.  Users without backup-mod won't see groups regardless.

Member resolution: each `ContactGroup.member_names` entry is split on
the first space into First/Last and looked up in ABPerson.  Members not
found are skipped with a debug log — this matches the additive-merge
philosophy (don't fail the run because one referenced contact is missing).
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path

from core.ios_backup_injector import IOSBackupInjector, get_current_injector
from core.normalization_schema import ContactGroup

logger = logging.getLogger(__name__)

_AB_DOMAIN = "HomeDomain"
_AB_RELPATH = "Library/AddressBook/AddressBook.sqlitedb"

# member_type values seen in ABGroupMembers.  0 = ABPerson reference.
_MEMBER_TYPE_PERSON = 0


def inject(
    device_id: str,
    items: list[ContactGroup],
    staging_dir: Path,
    is_privileged: bool,
) -> int:
    if not items:
        return 0

    injector = get_current_injector()
    if injector is None:
        logger.warning(
            "inject_contact_groups_ios: no active backup-mod session — "
            "iOS contact groups can't be injected via AFC, skipping %d "
            "group(s).", len(items),
        )
        return 0

    try:
        count = _inject_via_backup(injector, items)
        logger.info(
            "inject_contact_groups_ios: staged %d group(s) into the backup "
            "for %s", count, device_id,
        )
        return count
    except Exception:
        logger.exception(
            "inject_contact_groups_ios: backup-mod path failed for %s",
            device_id,
        )
        return 0


def _inject_via_backup(
    injector: IOSBackupInjector, items: list[ContactGroup]
) -> int:
    db_path = injector.stage_db(_AB_DOMAIN, _AB_RELPATH)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=FULL")

        # Use the first ABStore as the home for new groups (StoreID=1 is
        # the default local store on every reference backup we've seen).
        store_id = con.execute(
            "SELECT MIN(ROWID) FROM ABStore"
        ).fetchone()[0] or 1

        existing_titles: set[str] = set()
        for row in con.execute("SELECT Name FROM ABGroup"):
            if row[0]:
                existing_titles.add(str(row[0]))

        added = 0
        with con:
            for group in items:
                if not group.title or group.title in existing_titles:
                    continue
                group_uuid = str(uuid.uuid4()).upper()
                cur = con.execute(
                    "INSERT INTO ABGroup "
                    "(Name, StoreID, ExternalUUID, guid) "
                    "VALUES (?, ?, ?, ?)",
                    (group.title, store_id, group_uuid,
                     f"{group_uuid}:ABGroup"),
                )
                new_group_rowid = cur.lastrowid
                existing_titles.add(group.title)
                added += 1

                _link_members(con, new_group_rowid, group.member_names)
    finally:
        con.close()

    return added


def _link_members(
    con: sqlite3.Connection, group_rowid: int, member_names: list[str]
) -> int:
    linked = 0
    for raw in member_names or []:
        name = (raw or "").strip()
        if not name:
            continue
        first, _, last = name.partition(" ")
        first = first.strip() or None
        last = last.strip() or None
        person_rowid = _resolve_person(con, first, last)
        if person_rowid is None:
            logger.debug(
                "inject_contact_groups_ios: skipping unknown member %r "
                "for group_id=%d", name, group_rowid,
            )
            continue
        con.execute(
            "INSERT INTO ABGroupMembers (group_id, member_type, member_id) "
            "VALUES (?, ?, ?)",
            (group_rowid, _MEMBER_TYPE_PERSON, person_rowid),
        )
        linked += 1
    return linked


def _resolve_person(
    con: sqlite3.Connection, first: str | None, last: str | None
) -> int | None:
    if first is None and last is None:
        return None
    if first is not None and last is not None:
        row = con.execute(
            "SELECT ROWID FROM ABPerson WHERE First=? AND Last=? LIMIT 1",
            (first, last),
        ).fetchone()
    elif first is not None:
        row = con.execute(
            "SELECT ROWID FROM ABPerson WHERE First=? AND Last IS NULL LIMIT 1",
            (first,),
        ).fetchone()
    else:
        row = con.execute(
            "SELECT ROWID FROM ABPerson WHERE Last=? AND First IS NULL LIMIT 1",
            (last,),
        ).fetchone()
    return int(row[0]) if row else None
