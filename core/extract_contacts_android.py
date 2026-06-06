"""
extract_contacts_android.py

Extracts contacts from an Android device connected via ADB.

Two extraction paths:
- Non-rooted: uses Android content providers via `adb shell content query`.
- Rooted: copies the raw contacts2.db SQLite database to /sdcard/, pulls it
  locally, and queries it directly.  Falls back to the content provider path
  on any failure.

Returns a list of Contact objects as defined in normalization_schema.py.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

from core.adb_manager import ADBManager
from core.config_loader import get_config
from core.normalization_schema import Contact

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Content provider URIs and MIME type constants
# ---------------------------------------------------------------------------

_URI_RAW_CONTACTS = "content://com.android.contacts/raw_contacts"
_URI_DATA = "content://com.android.contacts/data"

_MIME_PHONE = "vnd.android.cursor.item/phone_v2"
_MIME_EMAIL = "vnd.android.cursor.item/email_v2"
_MIME_ORG = "vnd.android.cursor.item/organization"
_MIME_NAME = "vnd.android.cursor.item/name"

# Remote DB path and staging filename
_REMOTE_DB = "/data/data/com.android.providers.contacts/databases/contacts2.db"
_REMOTE_TMP = "/sdcard/contacts2_tmp.db"
_LOCAL_DB_NAME = "contacts2.db"

# Staging sub-directory name
_SUBDIR = "contacts_android"


# ---------------------------------------------------------------------------
# Row parser (shared across all four extractor modules)
# ---------------------------------------------------------------------------

def _parse_content_rows(output: str) -> list[dict[str, str]]:
    """
    Parse the stdout of `adb shell content query` into a list of dicts.

    Each output line looks like:
        Row: 0 _id=1, display_name=John Doe, account_type=com.google, ...

    Values may contain commas (e.g. display names), so we split on the
    pattern ", word_chars=" rather than on every comma.
    """
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("Row:"):
            continue
        # Strip "Row: N " prefix — two partition steps
        _, _, rest = line.partition(" ")   # drop "Row:"
        _, _, rest = rest.partition(" ")   # drop the row index number
        rest = rest.strip()
        if not rest:
            continue
        # Split on ", key=" boundaries so values with commas are preserved
        pairs = re.split(r',\s+(?=\w+=)', rest)
        row: dict[str, str] = {}
        for pair in pairs:
            k, _, v = pair.partition("=")
            row[k.strip()] = v.strip()
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(
    serial: str,
    staging_dir: Path,
    is_rooted: bool = False,
) -> list[Contact]:
    """
    Extract all contacts from the Android device identified by *serial*.

    Parameters
    ----------
    serial:      ADB device serial string.
    staging_dir: Root staging directory for this transfer session.
    is_rooted:   If True, attempt direct DB pull first (faster, more fields).

    Returns
    -------
    List of Contact objects; empty list on any fatal error.
    """
    try:
        sub = staging_dir / _SUBDIR
        sub.mkdir(parents=True, exist_ok=True)

        adb = ADBManager(get_config())

        if is_rooted:
            contacts = _extract_rooted(serial, sub, adb)
            if contacts is not None:
                logger.info(
                    "[contacts/android] Rooted path: extracted %d contacts",
                    len(contacts),
                )
                return contacts
            logger.warning(
                "[contacts/android] Rooted path failed, falling back to "
                "content provider"
            )

        contacts = _extract_content_provider(serial, adb)
        logger.info(
            "[contacts/android] Content provider path: extracted %d contacts",
            len(contacts),
        )
        return contacts

    except Exception:
        logger.exception("[contacts/android] Unhandled error during extraction")
        return []


# ---------------------------------------------------------------------------
# Non-rooted path — content provider
# ---------------------------------------------------------------------------

def _extract_content_provider(serial: str, adb: ADBManager) -> list[Contact]:
    """Query contacts2 via Android content providers (no root required)."""

    # --- Step 1: fetch raw_contacts to get contact_id → display_name map ----
    stdout, stderr, rc = adb.shell(
        serial,
        (
            "content query "
            f"--uri {_URI_RAW_CONTACTS} "
            "--projection _id,contact_id,display_name_primary,account_type"
        ),
        timeout=60,
    )
    if rc != 0:
        logger.warning(
            "[contacts/android] raw_contacts query failed (rc=%d): %s",
            rc, stderr,
        )
        return []

    raw_rows = _parse_content_rows(stdout)
    # Map contact_id → display_name_primary (fallback storage)
    contact_display: dict[str, str] = {}
    for row in raw_rows:
        cid = row.get("contact_id") or row.get("_id", "")
        name = row.get("display_name_primary", "")
        if cid:
            contact_display[cid] = name

    if not contact_display:
        logger.info("[contacts/android] No raw_contacts rows found")
        return []

    # --- Step 2: fetch data rows (phones, emails, name, org) ----------------
    stdout, stderr, rc = adb.shell(
        serial,
        (
            "content query "
            f"--uri {_URI_DATA} "
            "--projection contact_id,mimetype,data1,data2,data3,data5"
        ),
        timeout=120,
    )
    if rc != 0:
        logger.warning(
            "[contacts/android] data table query failed (rc=%d): %s", rc, stderr
        )
        # Return contacts with display names only — better than nothing
        return _display_names_to_contacts(contact_display)

    data_rows = _parse_content_rows(stdout)
    return _build_contacts(contact_display, data_rows)


# ---------------------------------------------------------------------------
# Rooted path — direct SQLite access
# ---------------------------------------------------------------------------

def _extract_rooted(
    serial: str,
    sub: Path,
    adb: ADBManager,
) -> list[Contact] | None:
    """
    Copy contacts2.db off the device, pull to staging, parse locally.
    Returns None on any failure so the caller can fall back.
    """
    local_db = sub / _LOCAL_DB_NAME

    # Copy DB to /sdcard/ so adb pull can reach it without root on host side
    _, _, rc = adb.shell_root(
        serial,
        f"cp {_REMOTE_DB} {_REMOTE_TMP}",
        timeout=30,
    )
    if rc != 0:
        logger.warning("[contacts/android] su cp failed (rc=%d)", rc)
        return None

    # Make it world-readable so adb pull works
    adb.shell_root(serial, f"chmod 644 {_REMOTE_TMP}", timeout=10)

    pulled = adb.pull_verified(serial, _REMOTE_TMP, local_db, timeout=60)

    # Clean up remote temp file regardless of pull result
    adb.shell(serial, f"rm -f {_REMOTE_TMP}", timeout=10)

    if not pulled or not local_db.exists():
        logger.warning("[contacts/android] adb pull of contacts DB failed")
        return None

    try:
        return _parse_sqlite_contacts(local_db)
    except Exception:
        logger.exception("[contacts/android] SQLite parse error")
        return None


def _parse_sqlite_contacts(db_path: Path) -> list[Contact]:
    """Open contacts2.db and extract contacts using direct SQL."""
    contacts: dict[str, dict] = {}  # contact_id -> aggregated data

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row

        # Build mimetype_id -> mimetype string lookup
        mime_map: dict[int, str] = {}
        try:
            for row in conn.execute("SELECT _id, mimetype FROM mimetypes"):
                mime_map[row["_id"]] = row["mimetype"]
        except sqlite3.OperationalError:
            logger.warning("[contacts/android] mimetypes table unavailable")

        # Load raw_contacts for contact_id and display name
        try:
            for row in conn.execute(
                "SELECT _id, contact_id, display_name_primary FROM raw_contacts"
            ):
                cid = str(row["contact_id"] or row["_id"])
                if cid not in contacts:
                    contacts[cid] = {
                        "display": row["display_name_primary"] or "",
                        "first": None,
                        "last": None,
                        "phones": [],
                        "emails": [],
                        "org": None,
                    }
        except sqlite3.OperationalError:
            logger.warning("[contacts/android] raw_contacts table unavailable")
            return []

        # Load data rows
        try:
            for row in conn.execute(
                "SELECT contact_id, mimetype_id, data1, data2, data3, data5 "
                "FROM data"
            ):
                cid = str(row["contact_id"])
                if cid not in contacts:
                    contacts[cid] = {
                        "display": "",
                        "first": None,
                        "last": None,
                        "phones": [],
                        "emails": [],
                        "org": None,
                    }
                mime = mime_map.get(row["mimetype_id"], "")
                d1 = row["data1"] or ""
                d2 = row["data2"] or ""
                d3 = row["data3"] or ""
                d5 = row["data5"] or ""
                _apply_data_row(contacts[cid], mime, d1, d2, d3, d5)
        except sqlite3.OperationalError:
            logger.warning("[contacts/android] data table unavailable")

    return _dict_to_contacts(contacts)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _build_contacts(
    contact_display: dict[str, str],
    data_rows: list[dict[str, str]],
) -> list[Contact]:
    """
    Combine the raw_contacts display map with data rows to build Contact list.
    """
    # Accumulator: contact_id -> mutable dict of fields
    accumulated: dict[str, dict] = {
        cid: {
            "display": name,
            "first": None,
            "last": None,
            "phones": [],
            "emails": [],
            "org": None,
        }
        for cid, name in contact_display.items()
    }

    for row in data_rows:
        cid = row.get("contact_id", "")
        if not cid:
            continue
        if cid not in accumulated:
            # Data row for a contact not in raw_contacts — still include it
            accumulated[cid] = {
                "display": "",
                "first": None,
                "last": None,
                "phones": [],
                "emails": [],
                "org": None,
            }
        mime = row.get("mimetype", "")
        d1 = row.get("data1", "")
        d2 = row.get("data2", "")
        d3 = row.get("data3", "")
        d5 = row.get("data5", "")
        _apply_data_row(accumulated[cid], mime, d1, d2, d3, d5)

    return _dict_to_contacts(accumulated)


def _apply_data_row(
    entry: dict,
    mime: str,
    d1: str,
    d2: str,
    d3: str,
    d5: str,
) -> None:
    """Mutate *entry* in-place based on the mimetype of a data row."""
    if mime == _MIME_PHONE:
        if d1 and d1 not in entry["phones"]:
            entry["phones"].append(d1)
    elif mime == _MIME_EMAIL:
        if d1 and d1 not in entry["emails"]:
            entry["emails"].append(d1)
    elif mime == _MIME_ORG:
        if d1 and entry["org"] is None:
            entry["org"] = d1
    elif mime == _MIME_NAME:
        # data2=first, data3=last, data5=display
        if d2 and entry["first"] is None:
            entry["first"] = d2
        if d3 and entry["last"] is None:
            entry["last"] = d3
        if d5 and not entry["display"]:
            entry["display"] = d5


def _dict_to_contacts(accumulated: dict[str, dict]) -> list[Contact]:
    """Convert the accumulated contact dicts to Contact dataclass instances."""
    results: list[Contact] = []
    for cid, entry in accumulated.items():
        first = entry.get("first")
        last = entry.get("last")

        # Fall back to splitting the display name if structured name is absent
        if not first and not last:
            display = entry.get("display", "").strip()
            if display:
                parts = display.split(maxsplit=1)
                first = parts[0]
                last = parts[1] if len(parts) > 1 else None

        results.append(
            Contact(
                first_name=first or None,
                last_name=last or None,
                phones=entry.get("phones", []),
                emails=entry.get("emails", []),
                organization=entry.get("org"),
            )
        )
    return results


def _display_names_to_contacts(
    contact_display: dict[str, str],
) -> list[Contact]:
    """
    Last-resort helper: build minimal Contact objects from display names only.
    Used when the data table query fails but raw_contacts succeeded.
    """
    results: list[Contact] = []
    for cid, display in contact_display.items():
        display = display.strip()
        parts = display.split(maxsplit=1) if display else []
        results.append(
            Contact(
                first_name=parts[0] if parts else None,
                last_name=parts[1] if len(parts) > 1 else None,
            )
        )
    return results
