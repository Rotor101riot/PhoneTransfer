"""
extract_contacts_ios.py

Extracts contacts from an iOS device and returns a list of Contact objects
defined in normalization_schema.py.

Strategy
--------
1. Prefer pymobiledevice3 address-book service (returns vCards directly).
2. If that fails, try AFC2 (jailbroken) to pull AddressBook.sqlitedb directly.
3. Fall back to iOSbackup for non-jailbroken devices.
4. Parse the SQLite DB: ABPerson + ABMultiValue tables.

Apple epoch: seconds since 2001-01-01 (not used for contacts, but kept as
a shared utility here).

Never raises — all exceptions are caught, logged, and result in a partial
or empty return value.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.normalization_schema import Contact

logger = logging.getLogger(__name__)

# AddressBook property IDs used in ABMultiValue
_PROP_PHONE   = 3
_PROP_EMAIL   = 4
_PROP_ADDRESS = 5
_PROP_URL     = 22

# Device path (AFC2 / iOSbackup)
_DB_DEVICE_PATH = "/var/mobile/Library/AddressBook/AddressBook.sqlitedb"
_DB_RELATIVE_PATH = "Library/AddressBook/AddressBook.sqlitedb"
_IOS_BACKUP_DOMAIN = "HomeDomain"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(udid: str, staging_dir: Path, is_jailbroken: bool = False) -> list[Contact]:
    """
    Extract all contacts from the iOS device identified by *udid*.

    Parameters
    ----------
    udid:           iOS device UDID.
    staging_dir:    Local directory used for temporary file copies.
    is_jailbroken:  Whether the device has AFC2 available.

    Returns
    -------
    list[Contact]   Possibly empty on total failure.
    """
    try:
        return _extract_impl(udid, staging_dir, is_jailbroken)
    except Exception as exc:
        logger.exception("extract_contacts_ios: top-level failure for %s: %s", udid, exc)
        return []


def _extract_impl(udid: str, staging_dir: Path, is_jailbroken: bool) -> list[Contact]:
    work_dir = staging_dir / "contacts_ios"
    work_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Attempt 1: pymobiledevice3 AddressBook service → vCards
    # ------------------------------------------------------------------
    contacts = _try_vcard_service(udid)
    if contacts:
        logger.info("contacts_ios: got %d contacts via vCard service", len(contacts))
        return contacts

    # ------------------------------------------------------------------
    # Attempt 2: pull DB via AFC2 (jailbroken) or iOSbackup (non-jailbroken)
    # ------------------------------------------------------------------
    db_path = _pull_addressbook_db(udid, work_dir, is_jailbroken)
    if db_path is None:
        logger.warning("contacts_ios: could not obtain AddressBook.sqlitedb for %s", udid)
        return []

    contacts = _parse_addressbook_db(db_path)
    logger.info("contacts_ios: parsed %d contacts from SQLite for %s", len(contacts), udid)
    return contacts


# ---------------------------------------------------------------------------
# Attempt 1 — pymobiledevice3 vCard service
# ---------------------------------------------------------------------------

def _try_vcard_service(udid: str) -> list[Contact]:
    """
    Try to export contacts via the pymobiledevice3 AddressBook service.
    Returns an empty list on any failure so the caller can fall back.
    """
    try:
        try:
            from core.device_connection_cache import get_lockdown
        except ImportError:
            logger.debug("contacts_ios: pymobiledevice3 not available for vCard service")
            return []

        lockdown = get_lockdown(udid)

        # The AddressBook service name varies by pymobiledevice3 version.
        for service_name in (
            "com.apple.mobilebackup",          # older path
            "com.apple.contacts.exportaddressbook",
        ):
            try:
                svc = lockdown.start_service(service_name)
                raw = svc.recv_plist()
                if raw:
                    return _parse_vcards(raw)
            except Exception:
                continue

        # pymobiledevice3 >= 4.x exposes AddressBookService directly
        try:
            from pymobiledevice3.services.contacts import ContactsService  # type: ignore[import]
            contacts_svc = ContactsService(lockdown)
            vcard_strings = contacts_svc.get_all_contacts()
            if vcard_strings:
                return _parse_vcards(vcard_strings)
        except Exception as exc:
            logger.debug("contacts_ios: ContactsService failed: %s", exc)

    except Exception as exc:
        logger.debug("contacts_ios: vCard service attempt failed: %s", exc)

    return []


def _parse_vcards(raw: Any) -> list[Contact]:
    """
    Parse a list of vCard strings (or a single combined blob) into Contact
    objects.  Uses vobject if available; falls back to naive text parsing.
    """
    contacts: list[Contact] = []

    # Normalise input to a list of strings
    if isinstance(raw, (bytes, str)):
        blobs = [raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw]
    elif isinstance(raw, (list, tuple)):
        blobs = []
        for item in raw:
            if isinstance(item, bytes):
                blobs.append(item.decode("utf-8", errors="replace"))
            elif isinstance(item, str):
                blobs.append(item)
    else:
        blobs = [str(raw)]

    # Split combined vCard blobs
    vcards: list[str] = []
    for blob in blobs:
        parts = blob.split("BEGIN:VCARD")
        for part in parts:
            part = part.strip()
            if part:
                vcards.append("BEGIN:VCARD\n" + part)

    for vcard_str in vcards:
        try:
            contact = _vcard_to_contact(vcard_str)
            contacts.append(contact)
        except Exception as exc:
            logger.debug("contacts_ios: failed to parse vCard: %s", exc)

    return contacts


def _vcard_to_contact(vcard_str: str) -> Contact:
    """Convert one vCard string to a Contact, using vobject if available."""
    try:
        import vobject  # type: ignore[import]
        vc = vobject.readOne(vcard_str)
        first = last = org = note = None
        phones: list[str] = []
        emails: list[str] = []

        if hasattr(vc, "n"):
            n = vc.n.value
            first = getattr(n, "given", None) or None
            last = getattr(n, "family", None) or None

        if hasattr(vc, "org"):
            org_val = vc.org.value
            if isinstance(org_val, (list, tuple)):
                org = " ".join(str(p) for p in org_val if p).strip() or None
            else:
                org = str(org_val).strip() or None

        if hasattr(vc, "note"):
            note = str(vc.note.value).strip() or None

        for tel in vc.contents.get("tel", []):
            val = str(tel.value).strip()
            if val:
                phones.append(val)

        for email in vc.contents.get("email", []):
            val = str(email.value).strip()
            if val:
                emails.append(val)

        return Contact(
            first_name=first,
            last_name=last,
            phones=phones,
            emails=emails,
            organization=org,
            note=note,
            raw_vcard=vcard_str,
        )

    except ImportError:
        # vobject not installed — naive line-by-line parse
        return _naive_vcard_parse(vcard_str)


def _naive_vcard_parse(vcard_str: str) -> Contact:
    """Very basic vCard parser for environments without vobject."""
    first = last = org = note = None
    phones: list[str] = []
    emails: list[str] = []

    for line in vcard_str.splitlines():
        line = line.strip()
        if not line or line.startswith("BEGIN:") or line.startswith("END:"):
            continue
        # Strip property params: TEL;TYPE=CELL:+1234 → value = +1234
        key_part, _, value = line.partition(":")
        value = value.strip()
        key = key_part.split(";")[0].upper()

        if key == "N":
            parts = value.split(";")
            last = parts[0].strip() or None
            first = parts[1].strip() if len(parts) > 1 else None
        elif key == "FN":
            pass  # prefer N breakdown
        elif key == "ORG":
            org = value.split(";")[0].strip() or None
        elif key == "NOTE":
            note = value or None
        elif key == "TEL":
            if value:
                phones.append(value)
        elif key == "EMAIL":
            if value:
                emails.append(value)

    return Contact(
        first_name=first,
        last_name=last,
        phones=phones,
        emails=emails,
        organization=org,
        note=note,
        raw_vcard=vcard_str,
    )


# ---------------------------------------------------------------------------
# Pull the AddressBook SQLite DB
# ---------------------------------------------------------------------------

def _pull_addressbook_db(udid: str, work_dir: Path, is_jailbroken: bool) -> Path | None:
    """
    Attempt to get AddressBook.sqlitedb onto the local filesystem.
    Returns the local Path on success, None on failure.
    """
    local_db = work_dir / "AddressBook.sqlitedb"

    # -- AFC2 (jailbroken) ---------------------------------------------------
    if is_jailbroken:
        try:
            from core.device_connection_cache import get_broker
            from core.afc2_connector import AFC2Connector

            broker = get_broker(udid)
            with AFC2Connector(broker) as afc2:
                ok = afc2.pull_file(_DB_DEVICE_PATH, local_db)
            if ok and local_db.exists():
                logger.debug("contacts_ios: pulled DB via AFC2")
                return local_db
        except PermissionError:
            logger.warning("contacts_ios: AFC2 not available despite is_jailbroken=True")
        except Exception as exc:
            logger.warning("contacts_ios: AFC2 pull failed: %s", exc)

    # -- iOSbackup (non-jailbroken) ------------------------------------------
    return _pull_via_iosbackup(udid, _DB_RELATIVE_PATH, _IOS_BACKUP_DOMAIN, local_db)


def _pull_via_iosbackup(udid: str, relative_path: str, domain: str, dest: Path) -> Path | None:
    """
    Pull a single file from an iOS backup using the iOSbackup library.
    Returns dest on success, None on failure.
    """
    try:
        from core.device_connection_cache import get_iosbackup
        dest.parent.mkdir(parents=True, exist_ok=True)
        backup = get_iosbackup(udid)
        info = backup.getFileDecryptedCopy(
            relativePath=relative_path,
            targetName=dest.name,
            targetFolder=str(dest.parent),
        )
        if info and dest.exists():
            logger.debug("contacts_ios: pulled %s via iOSbackup", relative_path)
            return dest
    except Exception as exc:
        logger.warning("contacts_ios: iOSbackup pull failed for %s: %s", relative_path, exc)

    return None


# ---------------------------------------------------------------------------
# Parse the AddressBook SQLite database
# ---------------------------------------------------------------------------

def _parse_addressbook_db(db_path: Path) -> list[Contact]:
    """
    Read AddressBook.sqlitedb and produce Contact objects.

    Tables used:
    - ABPerson         : pk, First, Middle, Last, Prefix, Suffix, Nickname,
                         Organization, Department, JobTitle, Note, Birthday
    - ABMultiValue     : record_id, property, uid, value
                         property 3=phone  4=email  5=address  22=URL
    - ABMultiValueEntry: parent_id, key, value  (structured address fields)
    """
    contacts: list[Contact] = []

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # Discover available ABPerson columns (schema varies by iOS version)
            try:
                col_info = conn.execute("PRAGMA table_info(ABPerson)").fetchall()
                abperson_cols = {r[1].upper() for r in col_info}
            except Exception:
                abperson_cols = set()

            def _has(col: str) -> bool:
                return col.upper() in abperson_cols

            select_fields = ["ROWID", "First", "Last", "Organization", "Note"]
            for extra in ("Middle", "Prefix", "Suffix", "Nickname",
                          "Department", "JobTitle", "Birthday"):
                if _has(extra):
                    select_fields.append(extra)

            # Load all persons
            try:
                cur.execute(
                    f"SELECT {', '.join(select_fields)} FROM ABPerson"
                )
                persons = cur.fetchall()
            except sqlite3.OperationalError as exc:
                logger.error("contacts_ios: ABPerson query failed: %s", exc)
                return []

            # Load multi-values: phones, emails, URLs
            phones_by_person: dict[int, list[str]] = {}
            emails_by_person: dict[int, list[str]] = {}
            urls_by_person:   dict[int, list[str]] = {}
            mv_uid_map:       dict[int, str] = {}   # mv ROWID -> uid (for address join)

            # uid column is absent on some iOS versions — detect before querying
            try:
                mv_col_info = conn.execute("PRAGMA table_info(ABMultiValue)").fetchall()
                mv_cols = {r[1].upper() for r in mv_col_info}
            except Exception:
                mv_cols = set()
            has_mv_uid = "UID" in mv_cols
            mv_select = (
                "ROWID, record_id, property, uid, value"
                if has_mv_uid else
                "ROWID, record_id, property, value"
            )

            try:
                cur.execute(
                    f"SELECT {mv_select} FROM ABMultiValue "
                    "WHERE property IN (?, ?, ?, ?)",
                    (_PROP_PHONE, _PROP_EMAIL, _PROP_ADDRESS, _PROP_URL),
                )
                for row in cur.fetchall():
                    try:
                        rid  = row["record_id"]
                        prop = row["property"]
                        val  = (row["value"] or "").strip()
                        try:
                            uid_val = (row["uid"] if has_mv_uid else None) or ""
                        except (IndexError, KeyError):
                            uid_val = ""
                        mv_uid_map[row["ROWID"]] = uid_val
                        if prop == _PROP_PHONE and val:
                            phones_by_person.setdefault(rid, []).append(val)
                        elif prop == _PROP_EMAIL and val:
                            emails_by_person.setdefault(rid, []).append(val)
                        elif prop == _PROP_URL and val:
                            urls_by_person.setdefault(rid, []).append(val)
                    except (IndexError, KeyError) as exc:
                        logger.debug("contacts_ios: skipping ABMultiValue row: %s", exc)
            except sqlite3.OperationalError as exc:
                logger.warning("contacts_ios: ABMultiValue query failed: %s", exc)

            # Load structured address components from ABMultiValueEntry
            # parent_id links back to ABMultiValue.ROWID
            addrs_by_person: dict[int, list[dict]] = {}
            try:
                cur.execute(
                    """
                    SELECT mv.record_id, mve.parent_id, mve.key, mve.value
                    FROM ABMultiValueEntry mve
                    JOIN ABMultiValue mv ON mv.ROWID = mve.parent_id
                    WHERE mv.property = ?
                    ORDER BY mv.record_id, mve.parent_id, mve.key
                    """,
                    (_PROP_ADDRESS,),
                )
                _addr_buf: dict[tuple, dict] = {}
                for row in cur.fetchall():
                    k = (row["record_id"], row["parent_id"])
                    _addr_buf.setdefault(k, {})[row["key"]] = row["value"] or ""
                for (rid, _), addr_parts in _addr_buf.items():
                    addrs_by_person.setdefault(rid, []).append(addr_parts)
            except sqlite3.OperationalError as exc:
                logger.debug("contacts_ios: ABMultiValueEntry not available: %s", exc)

            for row in persons:
                pk = row["ROWID"]

                # Build first name (include middle if present)
                first = row["First"] or None
                middle = row["Middle"] if _has("Middle") and row["Middle"] else None
                if first and middle:
                    first = f"{first} {middle}"
                elif middle:
                    first = middle

                note = row["Note"] or None
                org  = row["Organization"] or None
                phones = phones_by_person.get(pk, [])
                emails = emails_by_person.get(pk, [])

                # Build a proper vCard so extra fields survive round-trips
                vcard = _build_vcard(
                    first_name  = first,
                    last_name   = row["Last"] or None,
                    prefix      = row["Prefix"]  if _has("Prefix")  and row["Prefix"]  else None,
                    suffix      = row["Suffix"]  if _has("Suffix")  and row["Suffix"]  else None,
                    nickname    = row["Nickname"] if _has("Nickname") and row["Nickname"] else None,
                    org         = org,
                    dept        = row["Department"] if _has("Department") and row["Department"] else None,
                    title       = row["JobTitle"]   if _has("JobTitle")   and row["JobTitle"]   else None,
                    note        = note,
                    birthday    = row["Birthday"]   if _has("Birthday")   and row["Birthday"]   else None,
                    phones      = phones,
                    emails      = emails,
                    urls        = urls_by_person.get(pk, []),
                    addresses   = addrs_by_person.get(pk, []),
                )

                contact = Contact(
                    first_name  = first,
                    last_name   = row["Last"] or None,
                    organization= org,
                    note        = note,
                    phones      = phones,
                    emails      = emails,
                    raw_vcard   = vcard,
                )
                contacts.append(contact)

    except Exception as exc:
        logger.exception("contacts_ios: failed to parse AddressBook DB: %s", exc)

    return contacts


# Apple epoch for birthday conversion
_APPLE_EPOCH_DT = datetime(2001, 1, 1)


def _build_vcard(
    *,
    first_name: str | None,
    last_name:  str | None,
    prefix:     str | None,
    suffix:     str | None,
    nickname:   str | None,
    org:        str | None,
    dept:       str | None,
    title:      str | None,
    note:       str | None,
    birthday:   float | int | None,
    phones:     list[str],
    emails:     list[str],
    urls:       list[str],
    addresses:  list[dict],
) -> str:
    """
    Construct a vCard 3.0 string from individual contact fields.
    Stored in Contact.raw_vcard so injectors can reconstruct full fidelity.
    """
    lines = ["BEGIN:VCARD", "VERSION:3.0"]

    fn_parts = " ".join(p for p in [prefix, first_name, last_name, suffix] if p)
    if fn_parts:
        lines.append(f"FN:{fn_parts}")

    # N field: family;given;additional;prefix;suffix
    n_family  = last_name  or ""
    n_given   = first_name or ""
    lines.append(f"N:{n_family};{n_given};;;")

    if nickname:
        lines.append(f"NICKNAME:{nickname}")
    if org:
        org_field = org
        if dept:
            org_field += f";{dept}"
        lines.append(f"ORG:{org_field}")
    if title:
        lines.append(f"TITLE:{title}")
    if note:
        # Escape newlines per vCard spec
        safe_note = note.replace("\n", "\\n").replace("\r", "")
        lines.append(f"NOTE:{safe_note}")

    if birthday is not None:
        try:
            bday_dt = _APPLE_EPOCH_DT + timedelta(seconds=float(birthday))
            lines.append(f"BDAY:{bday_dt.strftime('%Y-%m-%d')}")
        except Exception:
            pass

    for phone in phones:
        lines.append(f"TEL;TYPE=VOICE:{phone}")
    for email in emails:
        lines.append(f"EMAIL:{email}")
    for url in urls:
        lines.append(f"URL:{url}")

    for addr in addresses:
        # vCard ADR: pobox;ext;street;city;state;zip;country
        street  = addr.get("Street",  addr.get("street",  ""))
        city    = addr.get("City",    addr.get("city",    ""))
        state   = addr.get("State",   addr.get("state",   ""))
        zip_    = addr.get("ZIP",     addr.get("zip",     addr.get("PostalCode", "")))
        country = addr.get("Country", addr.get("country", ""))
        lines.append(f"ADR;TYPE=HOME:;;{street};{city};{state};{zip_};{country}")

    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"
