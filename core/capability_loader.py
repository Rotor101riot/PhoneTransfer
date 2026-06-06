"""
capability_loader.py

Loads and queries the device capability map (ios_capability_map.json).

Used by the pipeline and UI to determine which categories are supported
for a given device platform and OS version — so the UI can grey out
categories that won't work, and the pipeline can skip them gracefully.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_MAP_PATH = Path(__file__).parent / "ios_capability_map.json"

# Fallback capability returned for an unknown iOS version.
_IOS_UNKNOWN: dict = {
    "heic_support": True,
    "live_afc_inject": True,
    "photos_schema_verified": False,
    "notes": "Unknown iOS version — runtime schema validation will run at transfer time",
}


@lru_cache(maxsize=1)
def _load_map() -> dict:
    """Load and cache the capability map JSON."""
    try:
        text = _MAP_PATH.read_text(encoding="utf-8")
        data = json.loads(text)
        logger.debug("capability_loader: loaded %s", _MAP_PATH)
        return data
    except Exception as exc:
        logger.warning("capability_loader: could not load %s: %s", _MAP_PATH, exc)
        return {}


# ---------------------------------------------------------------------------
# iOS capability queries
# ---------------------------------------------------------------------------

def get_ios_capabilities(ios_version: str) -> dict:
    """
    Return the capability dict for a given iOS version string (e.g. '17.2.1').

    Falls back to _IOS_UNKNOWN for versions not in the map.
    """
    data = _load_map()
    ios_map = data.get("ios", {})
    major = ios_version.split(".")[0] if ios_version else ""
    return ios_map.get(major, _IOS_UNKNOWN)


def photos_schema_verified(ios_version: str) -> bool:
    """Return True if the Photos.sqlite schema has been verified for this iOS version."""
    return bool(get_ios_capabilities(ios_version).get("photos_schema_verified", False))


# ---------------------------------------------------------------------------
# Category support queries
# ---------------------------------------------------------------------------

def category_supported(
    category: str,
    platform: str,
    version: str,
) -> bool:
    """
    Return True if a category is nominally supported for the given platform/version.

    Parameters
    ----------
    category : Category name (e.g. "contacts", "photos").
    platform : "ios" or "android".
    version  : OS version string (e.g. "17.2.1" or "14").

    Returns True (assume supported) for any category not present in the map,
    or when the version string cannot be parsed.
    """
    data = _load_map()
    cats = data.get("categories", {})
    entry = cats.get(category)
    if entry is None:
        return True  # unknown category — don't block it

    try:
        major = int(str(version).split(".")[0])
    except (ValueError, AttributeError):
        return True  # unparseable version — don't block it

    min_key = f"{platform}_min"
    min_ver = entry.get(min_key, 0)
    return major >= min_ver


def unsupported_categories(
    source_platform: str,
    source_version: str,
    dest_platform: str,
    dest_version: str,
    categories: list[str],
) -> dict[str, str]:
    """
    Return a dict of {category: reason} for categories that cannot be transferred.

    A category is blocked if it is unsupported on EITHER the source OR
    the destination device.

    Parameters
    ----------
    source_platform : "ios" or "android".
    source_version  : OS version string of the source device.
    dest_platform   : "ios" or "android".
    dest_version    : OS version string of the destination device.
    categories      : List of category names to check.

    Returns
    -------
    Dict mapping each unsupported category name to a short human-readable reason.
    Empty dict if all categories are supported.
    """
    blocked: dict[str, str] = {}
    for cat in categories:
        src_ok  = category_supported(cat, source_platform,  source_version)
        dst_ok  = category_supported(cat, dest_platform, dest_version)
        if not src_ok:
            try:
                min_v = _load_map()["categories"][cat][f"{source_platform}_min"]
            except (KeyError, TypeError):
                min_v = "?"
            blocked[cat] = (
                f"Requires {source_platform.upper()} {min_v}+  "
                f"(source is {source_version})"
            )
        elif not dst_ok:
            try:
                min_v = _load_map()["categories"][cat][f"{dest_platform}_min"]
            except (KeyError, TypeError):
                min_v = "?"
            blocked[cat] = (
                f"Requires {dest_platform.upper()} {min_v}+  "
                f"(destination is {dest_version})"
            )
    return blocked
