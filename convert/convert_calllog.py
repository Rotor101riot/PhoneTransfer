"""
convert_calllog.py

Normalizes and converts CallRecord objects.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from core.normalization_schema import CallRecord

# ---------------------------------------------------------------------------
# Android and iOS call-type maps
# ---------------------------------------------------------------------------

# Android: 1=incoming, 2=outgoing, 3=missed, 4=voicemail, 5=rejected, 6=blocked
# iOS CallKit: 1=incoming, 2=outgoing, 3=missed (similar mapping)
_INT_TYPE_MAP: dict[int, Literal["incoming", "outgoing", "missed"]] = {
    1: "incoming",
    2: "outgoing",
    3: "missed",
    4: "incoming",   # voicemail — treat as incoming
    5: "missed",     # rejected  — treat as missed
    6: "missed",     # blocked   — treat as missed
}

_STR_TYPE_MAP: dict[str, Literal["incoming", "outgoing", "missed"]] = {
    "incoming":  "incoming",
    "outgoing":  "outgoing",
    "missed":    "missed",
    "in":        "incoming",
    "out":       "outgoing",
    "received":  "incoming",
    "dialed":    "outgoing",
    "sent":      "outgoing",
    "rejected":  "missed",
    "blocked":   "missed",
    "voicemail": "incoming",
    "unknown":   "incoming",
}

_CSV_COLUMNS = ("number", "timestamp", "duration_seconds", "call_type", "name")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_call_type(raw: str | int) -> Literal["incoming", "outgoing", "missed"]:
    """
    Map Android/iOS integer types or string labels to canonical values.

    Android integers: 1=incoming, 2=outgoing, 3=missed.
    Unrecognized values default to ``"incoming"``.
    """
    if isinstance(raw, int):
        return _INT_TYPE_MAP.get(raw, "incoming")
    if isinstance(raw, str):
        # Try integer parse first
        try:
            return _INT_TYPE_MAP.get(int(raw), "incoming")
        except ValueError:
            pass
        return _STR_TYPE_MAP.get(raw.lower().strip(), "incoming")
    return "incoming"


def calls_to_dict(call: CallRecord) -> dict:
    """Serialize a CallRecord to a plain dict (``timestamp`` → ISO string)."""
    return {
        "number":           call.number,
        "timestamp":        call.timestamp.isoformat(),
        "duration_seconds": call.duration_seconds,
        "call_type":        call.call_type,
        "name":             call.name,
    }


def dict_to_call(d: dict) -> CallRecord:
    """Deserialize a CallRecord from a plain dict."""
    return CallRecord(
        number=d["number"],
        timestamp=datetime.fromisoformat(d["timestamp"]),
        duration_seconds=int(d["duration_seconds"]),
        call_type=normalize_call_type(d["call_type"]),
        name=d.get("name"),
    )


def calls_to_csv(calls: list[CallRecord], path: Path) -> Path:
    """
    Write a list of CallRecord objects to a CSV file at *path*.

    Columns: ``number``, ``timestamp`` (ISO), ``duration_seconds``, ``call_type``, ``name``.

    Returns *path*.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for call in calls:
            writer.writerow(calls_to_dict(call))
    return path


def csv_to_calls(path: Path) -> list[CallRecord]:
    """
    Read a CSV file written by :func:`calls_to_csv` and return a list of
    CallRecord objects.
    """
    path = Path(path)
    records: list[CallRecord] = []
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                records.append(dict_to_call(dict(row)))
            except Exception:
                # Skip malformed rows
                pass
    return records


def calls_to_json(calls: list[CallRecord], path: Path) -> Path:
    """Write calls to JSON. Returns *path*."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([calls_to_dict(c) for c in calls], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def calls_from_json(path: Path) -> list[CallRecord]:
    """Read calls from JSON written by :func:`calls_to_json`."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [dict_to_call(d) for d in raw]


def filter_calls(
    calls: list[CallRecord],
    types: list[str] | None = None,
    min_duration: int = 0,
) -> list[CallRecord]:
    """
    Filter a list of CallRecord objects.

    Args:
        calls:        Input list.
        types:        If provided, keep only records whose ``call_type`` is in
                      this list (values are normalized before comparison).
        min_duration: Keep only records with ``duration_seconds >= min_duration``.

    Returns:
        Filtered list (original objects, not copies).
    """
    result = calls
    if types is not None:
        canonical = {normalize_call_type(t) for t in types}
        result = [c for c in result if c.call_type in canonical]
    if min_duration > 0:
        result = [c for c in result if c.duration_seconds >= min_duration]
    return result
