"""
convert_contacts.py

Converts Contact objects to/from vCard 3.0 strings.
Used when injecting contacts into iOS (needs vCard) or parsing vCards from iOS backups.
"""

from __future__ import annotations

import quopri
import re
from pathlib import Path

from core.normalization_schema import Contact

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VCARD_ESCAPE = str.maketrans({
    "\\": "\\\\",
    ";":  "\\;",
    ",":  "\\,",
    "\n": "\\n",
    "\r": "",
})


def _escape(value: str) -> str:
    """Escape special characters per vCard 3.0 spec."""
    return value.translate(_VCARD_ESCAPE)


def _fold(line: str) -> str:
    """
    Fold a single vCard line to a maximum of 75 octets per line,
    continuing lines with a single space (RFC 6350 §3.2).
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line + "\r\n"

    result = []
    buf = b""
    for ch in line:
        ch_bytes = ch.encode("utf-8")
        if len(buf) + len(ch_bytes) > 75:
            result.append(buf.decode("utf-8"))
            buf = b" " + ch_bytes  # continuation lines start with a space
        else:
            buf += ch_bytes
    if buf:
        result.append(buf.decode("utf-8"))
    return "\r\n".join(result) + "\r\n"


def _unfold(vcard: str) -> str:
    """Unfold continued lines (CRLF + space or tab)."""
    return re.sub(r"\r\n[ \t]", "", vcard)


def normalize_phone(phone: str) -> str:
    """
    Strip all non-digit, non-+ characters.  Keep a leading +.

    Examples:
        "+1 (800) 555-1234" -> "+18005551234"
        "800-555-1234"      -> "8005551234"
    """
    stripped = re.sub(r"[^\d+]", "", phone)
    # Preserve at most one leading '+', remove any interior ones
    if stripped.startswith("+"):
        return "+" + re.sub(r"\+", "", stripped[1:])
    return re.sub(r"\+", "", stripped)


# ---------------------------------------------------------------------------
# Contact → vCard 3.0
# ---------------------------------------------------------------------------

def contact_to_vcard(contact: Contact) -> str:
    """
    Build a vCard 3.0 string from a Contact.

    If ``contact.raw_vcard`` is set and non-empty the raw string is returned
    unchanged (the caller already has a valid vCard).

    Returns a string with CRLF line endings, lines folded at 75 characters.
    """
    if contact.raw_vcard:
        return contact.raw_vcard

    lines: list[str] = []

    def add(line: str) -> None:
        lines.append(_fold(line))

    add("BEGIN:VCARD")
    add("VERSION:3.0")

    first = contact.first_name or ""
    last = contact.last_name or ""
    full = " ".join(part for part in (first, last) if part).strip()
    add(f"FN:{_escape(full)}")
    add(f"N:{_escape(last)};{_escape(first)};;;")

    for phone in contact.phones:
        add(f"TEL;TYPE=VOICE:{_escape(phone)}")

    for email in contact.emails:
        add(f"EMAIL;TYPE=INTERNET:{_escape(email)}")

    if contact.organization:
        add(f"ORG:{_escape(contact.organization)}")

    if contact.note:
        add(f"NOTE:{_escape(contact.note)}")

    add("END:VCARD")

    return "".join(lines)


# ---------------------------------------------------------------------------
# vCard 3.0 → Contact
# ---------------------------------------------------------------------------

def _decode_value(raw_value: str, params: dict[str, str]) -> str:
    """Decode QUOTED-PRINTABLE or BASE64 encoded vCard field values."""
    encoding = params.get("ENCODING", "").upper()
    if encoding == "QUOTED-PRINTABLE":
        try:
            return quopri.decodestring(raw_value.encode("utf-8")).decode("utf-8", errors="replace")
        except Exception:
            return raw_value
    if encoding in ("BASE64", "B"):
        import base64
        try:
            return base64.b64decode(raw_value).decode("utf-8", errors="replace")
        except Exception:
            return raw_value
    return raw_value


def _parse_params(param_str: str) -> dict[str, str]:
    """Parse the parameter portion of a vCard property line."""
    params: dict[str, str] = {}
    for part in param_str.split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            params[k.strip().upper()] = v.strip()
        elif part.strip():
            params[part.strip().upper()] = ""
    return params


def vcard_to_contact(vcard_str: str) -> Contact:
    """
    Parse a single vCard 3.0 (or 2.1) string into a Contact.

    Handles:
    - ENCODING=QUOTED-PRINTABLE and ENCODING=BASE64
    - N field split into first/last name
    - Multiple TEL and EMAIL lines
    """
    unfolded = _unfold(vcard_str)

    first_name: str | None = None
    last_name: str | None = None
    phones: list[str] = []
    emails: list[str] = []
    organization: str | None = None
    note: str | None = None

    for raw_line in unfolded.splitlines():
        line = raw_line.strip()
        if not line or line.upper() in ("BEGIN:VCARD", "END:VCARD", "VERSION:3.0", "VERSION:2.1"):
            continue

        # Split property name+params from value
        colon_idx = line.find(":")
        if colon_idx == -1:
            continue
        prop_part = line[:colon_idx]
        raw_value = line[colon_idx + 1:]

        # Separate property name from parameters
        prop_segments = prop_part.split(";")
        prop_name = prop_segments[0].upper()
        params = _parse_params(";".join(prop_segments[1:])) if len(prop_segments) > 1 else {}

        value = _decode_value(raw_value, params)
        # Unescape vCard escape sequences
        value = value.replace("\\n", "\n").replace("\\N", "\n")
        value = value.replace("\\\\", "\0BACKSLASH\0").replace("\\;", ";").replace("\\,", ",")
        value = value.replace("\0BACKSLASH\0", "\\")

        if prop_name == "N":
            parts = value.split(";")
            last_name = parts[0].strip() if len(parts) > 0 else None
            first_name = parts[1].strip() if len(parts) > 1 else None
            # Normalize empty strings to None
            last_name = last_name or None
            first_name = first_name or None

        elif prop_name == "TEL":
            norm = normalize_phone(value)
            if norm:
                phones.append(norm)

        elif prop_name == "EMAIL":
            stripped_email = value.strip()
            if stripped_email:
                emails.append(stripped_email)

        elif prop_name == "ORG":
            organization = value.split(";")[0].strip() or None

        elif prop_name == "NOTE":
            note = value.strip() or None

    return Contact(
        first_name=first_name,
        last_name=last_name,
        phones=phones,
        emails=emails,
        organization=organization,
        note=note,
        raw_vcard=vcard_str,
    )


# ---------------------------------------------------------------------------
# File-level helpers
# ---------------------------------------------------------------------------

def contacts_to_vcard_file(contacts: list[Contact], path: Path) -> Path:
    """
    Write multiple vCards (one per contact, separated by a blank line) to *path*.

    Returns *path*.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        for contact in contacts:
            fh.write(contact_to_vcard(contact))
    return path


def vcard_file_to_contacts(path: Path) -> list[Contact]:
    """
    Read a .vcf file, split on BEGIN:VCARD / END:VCARD boundaries,
    parse each block, and return a list of Contact objects.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")

    contacts: list[Contact] = []
    # Match each BEGIN:VCARD … END:VCARD block (case-insensitive, multiline)
    pattern = re.compile(
        r"BEGIN:VCARD\s*\r?\n.*?END:VCARD",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        try:
            contacts.append(vcard_to_contact(match.group(0)))
        except Exception:
            # Skip malformed blocks
            pass
    return contacts
