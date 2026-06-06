"""
inject_contacts_ios.py

Injects Contact records into an iOS device connected via USB.

Strategy
--------
All contacts are serialised to vCard 3.0 format and merged into a single
.vcf file.

Non-jailbroken path (standard AFC):
    The .vcf is pushed to /var/mobile/Media/PhoneTransfer/ via the standard
    AFC service (accessible without jailbreak).  The user then opens that file
    on the device to trigger the system import sheet in the Contacts app.

Jailbroken path (AFC2):
    The .vcf is written to
    /private/var/mobile/Library/AddressBook/AddressBookImport.vcf via AFC2.
    iOS will automatically pick it up the next time the Contacts app launches.

Return value: count of contacts whose vCard was successfully written to the
combined .vcf (which equals the contacts in the pushed file on success, or 0
on a total failure).
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

from core.afc_connector import AFCConnector
from core.afc2_connector import AFC2Connector
from core.ios_backup_injector import IOSBackupInjector, get_current_injector
from core.ios_service_broker import IOSServiceBroker
from core.normalization_schema import Contact

logger = logging.getLogger(__name__)

# Remote paths
_MEDIA_DIR = "/var/mobile/Media/PhoneTransfer"
_AFC2_ADDRESSBOOK_DIR = "/private/var/mobile/Library/AddressBook"
_AFC2_IMPORT_PATH = "/private/var/mobile/Library/AddressBook/AddressBookImport.vcf"

# AddressBook.sqlitedb schema constants (see G:/test/modify_contacts.py).
_ADDRESSBOOK_DOMAIN = "HomeDomain"
_ADDRESSBOOK_RELPATH = "Library/AddressBook/AddressBook.sqlitedb"
_APPLE_EPOCH_OFFSET = 978307200
_DEFAULT_STORE_ID = 1
_LABEL_HOME = 3
_LABEL_MOBILE = 4
_LABEL_WORK = 6
_PROPERTY_PHONE = 3
_PROPERTY_EMAIL = 4


# ---------------------------------------------------------------------------
# vCard serialisation
# ---------------------------------------------------------------------------

def _escape_vcard_value(value: str) -> str:
    """Escape special characters for vCard 3.0 field values."""
    value = value.replace("\\", "\\\\")
    value = value.replace(";", "\\;")
    value = value.replace(",", "\\,")
    value = value.replace("\n", "\\n")
    return value


def _contact_to_vcard(contact: Contact) -> str:
    """
    Convert a Contact dataclass to a vCard 3.0 string.

    If the contact already carries a raw_vcard blob we return that unchanged
    (it was likely produced by the source extractor and is already valid).
    Otherwise we build one from the structured fields.
    """
    if contact.raw_vcard:
        # Normalise line endings to CRLF as the vCard spec requires
        raw = contact.raw_vcard.replace("\r\n", "\n").replace("\r", "\n")
        return raw.replace("\n", "\r\n")

    lines: list[str] = ["BEGIN:VCARD", "VERSION:3.0"]

    first = (contact.first_name or "").strip()
    last = (contact.last_name or "").strip()

    if first or last:
        fn = " ".join(filter(None, [first, last]))
        n = f"{_escape_vcard_value(last)};{_escape_vcard_value(first)};;;"
        lines.append(f"FN:{_escape_vcard_value(fn)}")
        lines.append(f"N:{n}")
    else:
        # vCard 3.0 requires FN and N even if empty
        lines.append("FN:")
        lines.append("N:;;;;")

    for phone in contact.phones:
        lines.append(f"TEL;TYPE=VOICE:{_escape_vcard_value(phone)}")

    for email in contact.emails:
        lines.append(f"EMAIL;TYPE=INTERNET:{_escape_vcard_value(email)}")

    if contact.organization:
        lines.append(f"ORG:{_escape_vcard_value(contact.organization)}")

    if contact.note:
        lines.append(f"NOTE:{_escape_vcard_value(contact.note)}")

    lines.append("END:VCARD")
    return "\r\n".join(lines)


def _build_vcf(contacts: list[Contact]) -> tuple[str, int]:
    """
    Convert a list of Contacts into a single merged .vcf string.

    Returns (vcf_text, count_of_contacts_included).
    Contacts that fail serialisation are skipped with a logged warning.
    """
    parts: list[str] = []
    count = 0
    for i, contact in enumerate(contacts):
        try:
            vcard = _contact_to_vcard(contact)
            parts.append(vcard)
            count += 1
        except Exception as exc:
            logger.warning(
                "Failed to serialise contact %d (%r %r): %s",
                i,
                contact.first_name,
                contact.last_name,
                exc,
            )
    return "\r\n".join(parts), count


# ---------------------------------------------------------------------------
# Public inject function
# ---------------------------------------------------------------------------

def inject(
    udid: str,
    items: list[Contact],
    staging_dir: Path,
    is_jailbroken: bool = False,
) -> int:
    """
    Inject contacts into the iOS device identified by *udid*.

    Parameters
    ----------
    udid:           iOS device UDID.
    items:          Contacts to inject.
    staging_dir:    Local directory for temporary files.
    is_jailbroken:  When True, use AFC2 to write directly to the AddressBook
                    directory so iOS imports automatically on next app launch.
                    When False, push to /var/mobile/Media/PhoneTransfer/ and
                    instruct the user to open the file on-device.

    Returns
    -------
    int: Number of contacts successfully included in the pushed .vcf file.
         Returns 0 on a total (unrecoverable) failure.
    """
    if not items:
        logger.info("inject_contacts_ios: no contacts to inject — done.")
        return 0

    # Preferred path: a backup injector is active, so write directly into
    # AddressBook.sqlitedb.  This is strictly better than the AFC vCard push
    # because it requires no user action on the device.
    injector = get_current_injector()
    if injector is not None:
        try:
            count = _inject_via_backup(injector, items)
            logger.info(
                "inject_contacts_ios: staged %d contact(s) into the backup "
                "for %s", count, udid,
            )
            return count
        except Exception as exc:
            logger.warning(
                "inject_contacts_ios: backup-mod path failed (%s) — "
                "falling back to AFC vCard push", exc,
            )

    logger.info(
        "inject_contacts_ios: preparing %d contact(s) for device %s "
        "(jailbroken=%s)",
        len(items),
        udid,
        is_jailbroken,
    )

    # ── 1. Build the combined .vcf in the staging area ─────────────────────
    staging_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_vcf = staging_dir / f"contacts_{timestamp}.vcf"

    try:
        vcf_text, count = _build_vcf(items)
        if count == 0:
            logger.error(
                "inject_contacts_ios: every contact failed serialisation — aborting."
            )
            return 0

        local_vcf.write_text(vcf_text, encoding="utf-8")
        logger.debug(
            "inject_contacts_ios: wrote %d vCard(s) to staging file %s",
            count,
            local_vcf,
        )
    except Exception as exc:
        logger.error(
            "inject_contacts_ios: failed to write staging .vcf: %s", exc
        )
        return 0

    # ── 2. Push the .vcf to the device ─────────────────────────────────────
    broker = IOSServiceBroker(udid=udid)
    try:
        if is_jailbroken:
            return _push_jailbroken(broker, local_vcf, count, timestamp)
        else:
            return _push_standard(broker, local_vcf, count, timestamp)
    finally:
        broker.close()


# ---------------------------------------------------------------------------
# Push helpers
# ---------------------------------------------------------------------------

def _push_standard(
    broker: IOSServiceBroker,
    local_vcf: Path,
    count: int,
    timestamp: str,
) -> int:
    """
    Push the .vcf to /var/mobile/Media/PhoneTransfer/ via standard AFC.
    The user must open the file on the device to trigger the Contacts import.
    """
    logger.info(
        "inject_contacts_ios: non-jailbroken mode — pushing contacts.vcf to "
        "device Media folder.  Open the file on the device in the Contacts app "
        "to import."
    )
    try:
        afc = AFCConnector(broker)
    except Exception as exc:
        logger.error(
            "inject_contacts_ios: failed to open AFC service: %s", exc
        )
        return 0

    device_vcf = f"{_MEDIA_DIR}/contacts_{timestamp}.vcf"

    try:
        afc.makedirs(_MEDIA_DIR)
    except Exception as exc:
        logger.warning(
            "inject_contacts_ios: makedirs(%s) failed (may already exist): %s",
            _MEDIA_DIR,
            exc,
        )

    ok = afc.push_file(local_vcf, device_vcf)
    if ok:
        logger.info(
            "inject_contacts_ios: pushed %d contact(s) to %s — "
            "open this file on the device to import into Contacts.",
            count,
            device_vcf,
        )
        return count
    else:
        logger.error(
            "inject_contacts_ios: AFC push_file to %s failed.", device_vcf
        )
        return 0


def _push_jailbroken(
    broker: IOSServiceBroker,
    local_vcf: Path,
    count: int,
    timestamp: str,
) -> int:
    """
    Push the .vcf to /private/var/mobile/Library/AddressBook/ via AFC2.
    iOS will automatically import the file the next time the Contacts app opens.

    Falls back to the standard AFC path if AFC2 is unexpectedly unavailable.
    """
    logger.info(
        "inject_contacts_ios: jailbroken mode — writing contacts to "
        "AddressBook directory via AFC2 (%s).",
        _AFC2_IMPORT_PATH,
    )
    try:
        afc2 = AFC2Connector(broker)
    except PermissionError as exc:
        logger.warning(
            "inject_contacts_ios: AFC2 unavailable (%s) — "
            "falling back to standard AFC push.",
            exc,
        )
        return _push_standard(broker, local_vcf, count, timestamp)
    except Exception as exc:
        logger.error(
            "inject_contacts_ios: unexpected error opening AFC2 (%s) — "
            "falling back to standard AFC push.",
            exc,
        )
        return _push_standard(broker, local_vcf, count, timestamp)

    try:
        afc2.makedirs(_AFC2_ADDRESSBOOK_DIR)
    except Exception as exc:
        logger.warning(
            "inject_contacts_ios: makedirs(%s) failed: %s",
            _AFC2_ADDRESSBOOK_DIR,
            exc,
        )

    ok = afc2.push_file(local_vcf, _AFC2_IMPORT_PATH)
    if ok:
        logger.info(
            "inject_contacts_ios: pushed %d contact(s) to %s — "
            "iOS will import automatically on next Contacts app launch.",
            count,
            _AFC2_IMPORT_PATH,
        )
        return count
    else:
        logger.error(
            "inject_contacts_ios: AFC2 push_file to %s failed — "
            "falling back to standard AFC push.",
            _AFC2_IMPORT_PATH,
        )
        return _push_standard(broker, local_vcf, count, timestamp)


# ---------------------------------------------------------------------------
# Backup-mod path (preferred when an injector is active)
# ---------------------------------------------------------------------------

def _inject_via_backup(
    injector: IOSBackupInjector, contacts: list[Contact]
) -> int:
    """INSERT ABPerson + ABMultiValue rows into the staged AddressBook.sqlitedb."""
    db_path = injector.stage_db(_ADDRESSBOOK_DOMAIN, _ADDRESSBOOK_RELPATH)

    con = sqlite3.connect(str(db_path))
    try:
        # iOS triggers on ABPerson reference custom SQLite functions that
        # aren't available in desktop sqlite3.  Register no-op shims.
        con.create_function(
            "ab_update_value_from_trigger", 3,
            lambda cond, _col, _rowid: (1 if cond else 0),
            deterministic=False,
        )
        con.create_function(
            "ab_generate_guid", 0,
            lambda: str(uuid.uuid4()).upper(),
            deterministic=False,
        )
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=FULL")

        inserted = 0
        now = int(time.time()) - _APPLE_EPOCH_OFFSET

        with con:
            for c in contacts:
                if not (c.first_name or c.last_name or c.phones or c.emails):
                    continue

                person_uuid = str(uuid.uuid4()).upper()
                person_guid = f"{person_uuid}:ABPerson"
                first = (c.first_name or "").strip()
                last = (c.last_name or "").strip()

                cur = con.execute(
                    """
                    INSERT INTO ABPerson (
                        First, Last, Note, Kind,
                        FirstSort, LastSort,
                        CreationDate, ModificationDate,
                        ExternalIdentifier, ExternalUUID, StoreID,
                        FirstSortSection, LastSortSection,
                        FirstSortLanguageIndex, LastSortLanguageIndex,
                        PersonLink, IsPreferredName,
                        guid, DisplayFlags
                    ) VALUES (
                        ?, ?, ?, 0,
                        ?, ?,
                        ?, ?,
                        ?, ?, ?,
                        ?, ?,
                        2147483647, 2147483647,
                        -1, 1,
                        ?, 0
                    )
                    """,
                    (
                        first or None,
                        last or None,
                        c.note,
                        first.upper(),
                        last.upper(),
                        now,
                        now,
                        person_uuid,
                        person_uuid,
                        _DEFAULT_STORE_ID,
                        first[:1].upper(),
                        last[:1].upper(),
                        person_guid,
                    ),
                )
                person_id = cur.lastrowid

                for phone in c.phones:
                    con.execute(
                        "INSERT INTO ABMultiValue "
                        "(record_id, property, identifier, label, value, guid) "
                        "VALUES (?, ?, 0, ?, ?, ?)",
                        (person_id, _PROPERTY_PHONE, _LABEL_MOBILE,
                         phone, str(uuid.uuid4()).upper()),
                    )

                for email in c.emails:
                    con.execute(
                        "INSERT INTO ABMultiValue "
                        "(record_id, property, identifier, label, value, guid) "
                        "VALUES (?, ?, 0, ?, ?, ?)",
                        (person_id, _PROPERTY_EMAIL, _LABEL_HOME,
                         email, str(uuid.uuid4()).upper()),
                    )

                if c.organization:
                    con.execute(
                        "UPDATE ABPerson SET Organization=? WHERE ROWID=?",
                        (c.organization, person_id),
                    )

                inserted += 1
    finally:
        con.close()

    return inserted
