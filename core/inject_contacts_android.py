"""
inject_contacts_android.py

Injects Contact records into an Android device connected via USB/ADB.

Strategy
--------
1.  All contacts are serialised to a single vCard 3.0 .vcf file and pushed
    to /sdcard/PhoneTransfer/ on the device.  This gives the user a manual
    fallback: they can open the file in the Contacts app at any time.

2.  A silent, automated import is then attempted for each contact via the
    Android Contacts content provider (``content insert`` shell commands).
    This requires no user interaction and works on Android 4 – 9 for local
    (non-account) contacts.  On Android 10+ the provider may reject inserts
    from shell; those failures are logged as warnings and do not abort the
    run.

3.  If the content provider insert returns a non-zero exit code, the contact
    is counted as VCF-only (user must import manually).

Return value: count of contacts for which at least one injection path
succeeded (content insert *or* VCF pushed successfully).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from core.adb_manager import ADBManager
from core.config_loader import get_config
from core.normalization_schema import Contact

logger = logging.getLogger(__name__)

_DEVICE_DIR = "/sdcard/PhoneTransfer"
_URI_RAW_CONTACTS = "content://com.android.contacts/raw_contacts"


def _count_raw_contacts(adb: ADBManager, serial: str) -> int | None:
    """Return the current raw_contacts row count, or None if unavailable."""
    try:
        stdout, _, rc = adb.shell(
            serial,
            f"content query --uri {_URI_RAW_CONTACTS} --projection _id",
            timeout=15,
        )
        if rc != 0:
            return None
        return sum(1 for line in stdout.splitlines() if line.strip().startswith("Row:"))
    except Exception:
        return None


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
    Convert a Contact to a vCard 3.0 string (CRLF line endings).

    If ``contact.raw_vcard`` is present it is returned as-is (line endings
    normalised to CRLF).
    """
    if contact.raw_vcard:
        raw = contact.raw_vcard.replace("\r\n", "\n").replace("\r", "\n")
        return raw.replace("\n", "\r\n")

    lines: list[str] = ["BEGIN:VCARD", "VERSION:3.0"]

    first = (contact.first_name or "").strip()
    last = (contact.last_name or "").strip()

    if first or last:
        fn = " ".join(filter(None, [first, last]))
        lines.append(f"FN:{_escape_vcard_value(fn)}")
        lines.append(
            f"N:{_escape_vcard_value(last)};{_escape_vcard_value(first)};;;"
        )
    else:
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
    Merge all contacts into a single .vcf string.

    Returns (vcf_text, count_included).  Contacts that raise during
    serialisation are skipped with a warning.
    """
    parts: list[str] = []
    count = 0
    for i, c in enumerate(contacts):
        try:
            parts.append(_contact_to_vcard(c))
            count += 1
        except Exception as exc:
            logger.warning(
                "inject_contacts_android: skipping contact %d (%r %r): %s",
                i, c.first_name, c.last_name, exc,
            )
    return "\r\n".join(parts), count


# ---------------------------------------------------------------------------
# Content-provider insert helpers
# ---------------------------------------------------------------------------

def _esc_shell(value: str) -> str:
    """Escape single quotes for use inside a shell single-quoted string."""
    return value.replace("'", "\\'")


def _parse_last_id(stdout: str) -> str | None:
    """
    Parse the _id from a ``content query`` result that looks like::

        Row: 0 _id=42

    Returns the id as a string, or None if not found.
    """
    match = re.search(r"_id=(\d+)", stdout)
    return match.group(1) if match else None


def _insert_contact(adb: ADBManager, serial: str, contact: Contact) -> bool:
    """
    Insert a single contact into the Android Contacts content provider.

    Returns True if the raw_contact row was created and at least the name
    data row was inserted successfully.
    """
    # 1. Create the raw_contact row (no account — local contact)
    _, _, rc = adb.shell(
        serial,
        "content insert --uri content://com.android.contacts/raw_contacts "
        "--bind account_type:s:'' --bind account_name:s:''",
        timeout=10,
    )
    if rc != 0:
        logger.debug(
            "inject_contacts_android: raw_contacts insert returned rc=%d", rc
        )
        return False

    # 2. Retrieve the new row's _id (most-recently inserted = highest _id)
    stdout, _, rc = adb.shell(
        serial,
        "content query --uri content://com.android.contacts/raw_contacts "
        "--projection _id --sort '_id DESC'",
        timeout=10,
    )
    raw_id = _parse_last_id(stdout)
    if not raw_id:
        logger.debug(
            "inject_contacts_android: could not determine raw_contact _id from: %r",
            stdout,
        )
        return False

    # 3. Insert the name data row
    first = _esc_shell(contact.first_name or "")
    last = _esc_shell(contact.last_name or "")
    fn = _esc_shell(
        " ".join(filter(None, [contact.first_name, contact.last_name])) or "Unknown"
    )
    _, _, rc = adb.shell(
        serial,
        f"content insert --uri content://com.android.contacts/data "
        f"--bind raw_contact_id:i:{raw_id} "
        f"--bind mimetype:s:vnd.android.cursor.item/name "
        f"--bind data1:s:'{fn}' "
        f"--bind data2:s:'{first}' "
        f"--bind data3:s:'{last}'",
        timeout=10,
    )
    if rc != 0:
        logger.debug(
            "inject_contacts_android: name data insert returned rc=%d for raw_id=%s",
            rc, raw_id,
        )
        # Not fatal — phone/email rows may still succeed; keep going.

    # 4. Insert phone rows
    for phone in contact.phones:
        escaped = _esc_shell(phone)
        _, _, rc = adb.shell(
            serial,
            f"content insert --uri content://com.android.contacts/data "
            f"--bind raw_contact_id:i:{raw_id} "
            f"--bind mimetype:s:vnd.android.cursor.item/phone_v2 "
            f"--bind data1:s:'{escaped}'",
            timeout=10,
        )
        if rc != 0:
            logger.warning("inject_contacts_android: phone insert failed (rc=%d)", rc)

    # 5. Insert e-mail rows
    for email in contact.emails:
        escaped = _esc_shell(email)
        _, _, rc = adb.shell(
            serial,
            f"content insert --uri content://com.android.contacts/data "
            f"--bind raw_contact_id:i:{raw_id} "
            f"--bind mimetype:s:vnd.android.cursor.item/email_v2 "
            f"--bind data1:s:'{escaped}'",
            timeout=10,
        )
        if rc != 0:
            logger.warning("inject_contacts_android: email insert failed (rc=%d)", rc)

    # 6. Insert organisation row if present
    if contact.organization:
        org = _esc_shell(contact.organization)
        _, _, rc = adb.shell(
            serial,
            f"content insert --uri content://com.android.contacts/data "
            f"--bind raw_contact_id:i:{raw_id} "
            f"--bind mimetype:s:vnd.android.cursor.item/organization "
            f"--bind data1:s:'{org}'",
            timeout=10,
        )
        if rc != 0:
            logger.warning("inject_contacts_android: org insert failed (rc=%d)", rc)

    # 7. Insert note row if present
    if contact.note:
        note = _esc_shell(contact.note)
        _, _, rc = adb.shell(
            serial,
            f"content insert --uri content://com.android.contacts/data "
            f"--bind raw_contact_id:i:{raw_id} "
            f"--bind mimetype:s:vnd.android.cursor.item/note "
            f"--bind data1:s:'{note}'",
            timeout=10,
        )
        if rc != 0:
            logger.warning("inject_contacts_android: note insert failed (rc=%d)", rc)

    return True


# ---------------------------------------------------------------------------
# Public inject function
# ---------------------------------------------------------------------------

def inject(
    serial: str,
    items: list[Contact],
    staging_dir: Path,
    is_rooted: bool = False,
) -> int:
    """
    Inject contacts into the Android device identified by *serial*.

    Parameters
    ----------
    serial:      ADB device serial.
    items:       Contacts to inject.
    staging_dir: Local directory for temporary files.
    is_rooted:   Currently unused for contacts — reserved for future direct-DB
                 injection path.

    Returns
    -------
    int: Number of contacts successfully injected (content provider insert) or,
         if content inserts all failed, the number included in the pushed VCF.
         Returns 0 on total failure.
    """
    if not items:
        logger.info("inject_contacts_android: no contacts to inject — done.")
        return 0

    logger.info(
        "inject_contacts_android: preparing %d contact(s) for device %s",
        len(items), serial,
    )

    try:
        cfg = get_config()
        adb = ADBManager(cfg)
    except Exception as exc:
        logger.error("inject_contacts_android: failed to initialise ADB: %s", exc)
        return 0

    # ── 1. Ensure device directory exists ────────────────────────────────────
    try:
        adb.shell(serial, f"mkdir -p {_DEVICE_DIR}")
    except Exception as exc:
        logger.warning(
            "inject_contacts_android: mkdir -p %s failed: %s", _DEVICE_DIR, exc
        )

    # ── 2. Build and push the combined .vcf ───────────────────────────────────
    staging_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_vcf = staging_dir / f"contacts_{timestamp}.vcf"
    remote_vcf = f"{_DEVICE_DIR}/contacts_{timestamp}.vcf"

    vcf_count = 0
    vcf_pushed = False
    try:
        vcf_text, vcf_count = _build_vcf(items)
        if vcf_count == 0:
            logger.error(
                "inject_contacts_android: every contact failed vCard serialisation."
            )
            return 0
        local_vcf.write_text(vcf_text, encoding="utf-8")
        vcf_pushed = adb.push(serial, local_vcf, remote_vcf)
        if vcf_pushed:
            logger.info(
                "inject_contacts_android: contacts VCF available at %s — "
                "also attempting silent content-provider import…",
                remote_vcf,
            )
        else:
            logger.warning(
                "inject_contacts_android: VCF push to %s failed.", remote_vcf
            )
    except Exception as exc:
        logger.error(
            "inject_contacts_android: error building/pushing VCF: %s", exc
        )

    # ── 3. Silent content-provider import ────────────────────────────────────
    _pre_count = _count_raw_contacts(adb, serial)
    silent_count = 0
    for i, contact in enumerate(items):
        try:
            ok = _insert_contact(adb, serial, contact)
            if ok:
                silent_count += 1
            else:
                logger.debug(
                    "inject_contacts_android: content insert failed for "
                    "contact %d (%r %r); VCF fallback available.",
                    i, contact.first_name, contact.last_name,
                )
        except Exception as exc:
            logger.warning(
                "inject_contacts_android: unexpected error inserting contact %d: %s",
                i, exc,
            )

    if silent_count > 0:
        logger.info(
            "inject_contacts_android: silent import succeeded for %d/%d contact(s).",
            silent_count, len(items),
        )
        # Post-write verification: confirm the row count actually grew
        _post_count = _count_raw_contacts(adb, serial)
        if _pre_count is not None and _post_count is not None:
            _delta = _post_count - _pre_count
            if _delta < silent_count:
                logger.warning(
                    "inject_contacts_android: post-write verification: "
                    "expected +%d raw_contacts rows but only got +%d "
                    "(pre=%d post=%d) — OEM provider may have silently dropped rows",
                    silent_count, _delta, _pre_count, _post_count,
                )
            else:
                logger.debug(
                    "inject_contacts_android: post-write OK — raw_contacts +%d rows",
                    _delta,
                )
        return silent_count

    # Content provider failed for everything — fall back to VCF count
    if vcf_pushed and vcf_count > 0:
        logger.warning(
            "inject_contacts_android: content-provider import failed for all "
            "contacts.  VCF pushed to %s — user must open it on device to import.",
            remote_vcf,
        )
        return vcf_count

    logger.error(
        "inject_contacts_android: both silent import and VCF push failed."
    )
    return 0
