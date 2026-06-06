"""
reference/enrich_device_lookups.py

One-shot script to download and merge online device databases into the
local reference JSON files.

Sources:
  Android â€” Google Play supported devices CSV (50k+ devices, all brands)
             https://storage.googleapis.com/play_public/supported_devices.csv
  iOS     â€” appledb.dev main.json.gz (MIT, all Apple identifiers up to 2026)
             https://api.appledb.dev/device/main.json.gz

Run:
    python reference/enrich_device_lookups.py

Outputs:
    reference/android_device_lookup.json  (merged: dr.fone Samsung + Google Play all brands)
    reference/device_lookup.json          (merged: 3uTools iOS + appledb identifiers)
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import sys
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

_log = logging.getLogger(__name__)

# Only reconfigure stdout when running as a standalone script
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REF = Path(__file__).parent

ANDROID_OUT = REF / "android_device_lookup.json"
IOS_OUT     = REF / "device_lookup.json"

PLAY_CSV_URL    = "https://storage.googleapis.com/play_public/supported_devices.csv"
APPLEDB_URL     = "https://api.appledb.dev/device/main.json.gz"

APPLEDB_TYPES = {
    "iPhone", "iPad", "iPad Pro", "iPad Air", "iPad mini",
    "iPod touch", "Apple Watch", "Apple TV", "HomePod",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


_IS_SCRIPT = __name__ == "__main__"


def _log_or_print(msg: str) -> None:
    """Route output to stdout when run as a script, to logger when imported."""
    if _IS_SCRIPT:
        print(msg)
    else:
        _log.debug(msg)


# ---------------------------------------------------------------------------
# Android â€” Google Play CSV
# ---------------------------------------------------------------------------

def enrich_android() -> None:
    _log_or_print("Fetching Google Play supported devices CSV...")
    raw = _fetch(PLAY_CSV_URL)
    text = raw.decode("utf-16", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    # Vote on the most common marketing name per model number
    votes: defaultdict[str, Counter] = defaultdict(Counter)
    brand_for: dict[str, str] = {}

    for row in reader:
        model   = (row.get("Model") or "").strip()
        name    = (row.get("Marketing Name") or "").strip()
        branding = (row.get("Retail Branding") or "").strip()
        if not model or not name:
            continue
        votes[model][name] += 1
        if branding and model not in brand_for:
            brand_for[model] = branding

    play_map: dict[str, str] = {
        model: ctr.most_common(1)[0][0]
        for model, ctr in votes.items()
    }
    _log_or_print(f"  Google Play CSV: {len(play_map):,} distinct model numbers")

    # Load existing (dr.fone Samsung) and merge â€” existing data wins on conflict
    existing: dict[str, str] = {}
    if ANDROID_OUT.exists():
        existing = json.loads(ANDROID_OUT.read_text(encoding="utf-8")).get("devices", {})
        _log_or_print(f"  Existing android_device_lookup.json: {len(existing):,} entries")

    # Merge: existing takes priority (dr.fone Samsung names are high-quality)
    merged = {**play_map, **existing}
    _log_or_print(f"  Merged total: {len(merged):,} entries")

    # Count manufacturers from Play data
    mfr_counter: Counter = Counter()
    for model in play_map:
        mfr = brand_for.get(model, "Unknown")
        mfr_counter[mfr] += 1
    top_mfr = mfr_counter.most_common(10)
    _log_or_print(f"  Top manufacturers: {top_mfr}")

    out = {
        "_source": (
            "Merged: Wondershare dr.fone repair.mapping.db (Samsung, 2004 models) "
            "+ Google Play supported_devices.csv (all brands, 50k+ rows)"
        ),
        "_note": "model -> marketing name",
        "devices": merged,
    }
    ANDROID_OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    _log_or_print(f"  Written: {ANDROID_OUT} ({ANDROID_OUT.stat().st_size // 1024} KB)\n")


# ---------------------------------------------------------------------------
# iOS â€” appledb.dev
# ---------------------------------------------------------------------------

def enrich_ios() -> None:
    _log_or_print("Fetching appledb.dev main.json.gz...")
    raw = _fetch(APPLEDB_URL)
    data: list[dict] = json.loads(gzip.decompress(raw))
    _log_or_print(f"  appledb: {len(data)} total entries")

    # Build identifier -> name from relevant device types
    appledb_map: dict[str, str] = {}
    for entry in data:
        if entry.get("type") not in APPLEDB_TYPES:
            continue
        name = entry.get("name", "")
        for ident in (entry.get("identifier") or []):
            if ident:
                appledb_map[ident] = name

    _log_or_print(f"  appledb identifiers extracted: {len(appledb_map)}")
    iphone_ids = [k for k in appledb_map if k.startswith("iPhone")]
    _log_or_print(f"  iPhone identifiers: {len(iphone_ids)}")
    # Show recent iPhones
    recent = [(k, v) for k, v in appledb_map.items()
              if any(k.startswith(f"iPhone{n},") for n in ["15","16","17","18"])]
    if recent:
        _log_or_print(f"  Recent iPhones (15-18 series): {sorted(recent)}")

    # Load existing device_lookup.json (3uTools, 190 entries with rich fields)
    existing_data: dict = {}
    if IOS_OUT.exists():
        existing_data = json.loads(IOS_OUT.read_text(encoding="utf-8"))
    existing_devices: dict[str, dict] = existing_data.get("devices", {})
    _log_or_print(f"  Existing device_lookup.json: {len(existing_devices)} entries")

    # Add appledb entries that are missing or update name only for existing entries
    added = 0
    updated = 0
    for ident, name in appledb_map.items():
        if ident not in existing_devices:
            # New entry: minimal record with just the name
            existing_devices[ident] = {"name": name}
            added += 1
        elif existing_devices[ident].get("name") != name:
            # Update name if appledb differs (newer/more accurate)
            existing_devices[ident]["name"] = name
            updated += 1

    _log_or_print(f"  Added {added} new identifiers, updated {updated} names")
    _log_or_print(f"  Total iOS entries: {len(existing_devices)}")

    out = {
        **existing_data,
        "_source": (
            "Merged: 3uTools devices_table.txt v2026.03.06.02 "
            "+ appledb.dev main.json.gz (MIT, up to 2026)"
        ),
        "_note": "productType -> device info. fingerprint/faceId are bool. network is list.",
        "devices": existing_devices,
    }
    IOS_OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    _log_or_print(f"  Written: {IOS_OUT} ({IOS_OUT.stat().st_size // 1024} KB)\n")


# ---------------------------------------------------------------------------
# Public API (callable from background threads in the main app)
# ---------------------------------------------------------------------------

def enrich_all() -> tuple[bool, bool]:
    """
    Fetch the latest Android and iOS device databases and merge them into the
    local reference JSON files.  Intended to be called from a background
    thread on app launch so the lookup data stays current without blocking
    the UI.

    Returns
    -------
    tuple[android_ok, ios_ok]
        Each element is True if that enrichment succeeded, False on error.
    """
    android_ok = False
    ios_ok     = False

    try:
        enrich_android()
        android_ok = True
    except Exception as exc:
        _log.warning("enrich_device_lookups: Android update failed: %s", exc)

    try:
        enrich_ios()
        ios_ok = True
    except Exception as exc:
        _log.warning("enrich_device_lookups: iOS update failed: %s", exc)

    return android_ok, ios_ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        enrich_android()
    except Exception as exc:
        _log_or_print(f"Android enrichment failed: {exc}")

    try:
        enrich_ios()
    except Exception as exc:
        _log_or_print(f"iOS enrichment failed: {exc}")

    _log_or_print("Done.")
