"""
convert_blocked.py

Normalizes and converts BlockedNumber lists.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from core.normalization_schema import BlockedNumber

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_number(number: str) -> str:
    """
    Normalize a phone number to a canonical form.

    - Keep only digits and a single leading ``+``.
    - Strip spaces, dashes, parentheses, and any other non-digit characters.

    Examples::

        "+1 (800) 555-1234" -> "+18005551234"
        "800 555 1234"      -> "8005551234"
    """
    stripped = re.sub(r"[^\d+]", "", number)
    if stripped.startswith("+"):
        return "+" + re.sub(r"\+", "", stripped[1:])
    return re.sub(r"\+", "", stripped)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def blocked_to_dict(b: BlockedNumber) -> dict:
    """Serialize a BlockedNumber to a plain dict."""
    return {
        "number": b.number,
        "name":   b.name,
    }


def dict_to_blocked(d: dict) -> BlockedNumber:
    """Deserialize a BlockedNumber from a plain dict."""
    return BlockedNumber(
        number=d["number"],
        name=d.get("name"),
    )


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

_CSV_COLUMNS = ("number", "name")


def blocked_to_csv(items: list[BlockedNumber], path: Path) -> Path:
    """
    Write a list of BlockedNumber objects to a CSV file at *path*.

    Columns: ``number``, ``name``.  Returns *path*.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for item in items:
            writer.writerow({"number": item.number, "name": item.name or ""})
    return path


def csv_to_blocked(path: Path) -> list[BlockedNumber]:
    """
    Read a CSV file written by :func:`blocked_to_csv` and return a list of
    BlockedNumber objects.

    The ``name`` field defaults to ``None`` if absent or empty.
    """
    path = Path(path)
    items: list[BlockedNumber] = []
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            name = row.get("name", "").strip() or None
            number = row.get("number", "").strip()
            if number:
                items.append(BlockedNumber(number=number, name=name))
    return items


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def blocked_to_json(items: list[BlockedNumber], path: Path) -> Path:
    """Write blocked numbers to a JSON file. Returns *path*."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([blocked_to_dict(b) for b in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def blocked_from_json(path: Path) -> list[BlockedNumber]:
    """Read blocked numbers from a JSON file."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [dict_to_blocked(d) for d in raw]


# ---------------------------------------------------------------------------
# Merge helper
# ---------------------------------------------------------------------------

def merge_blocked(
    existing: list[BlockedNumber],
    incoming: list[BlockedNumber],
) -> list[BlockedNumber]:
    """
    Append blocked numbers from *incoming* that are not already in *existing*.

    Deduplication is based on the normalized phone number.

    Returns the merged list (existing items first, then new ones).
    """
    existing_normalized: set[str] = {normalize_number(b.number) for b in existing}
    result = list(existing)
    for b in incoming:
        norm = normalize_number(b.number)
        if norm not in existing_normalized:
            existing_normalized.add(norm)
            result.append(b)
    return result
