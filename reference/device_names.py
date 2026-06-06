"""
reference/device_names.py

Resolves raw device model identifiers to human-readable marketing names.

iOS
---
Maps Apple ProductType strings (e.g. "iPhone15,3") to marketing names
(e.g. "iPhone 14 Pro Max") using reference/device_lookup.json.

Android
-------
Android model strings from ro.product.model are already human-readable for
some manufacturers (Pixel 7 Pro, Galaxy S21) but opaque for others
(SM-G991B, CPH2423).  When a brand string is available it is prepended with
title-casing so at minimum the display reads "Samsung SM-G991B" rather than
the raw build name like "beyond1q".
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_IOS_LOOKUP_PATH     = Path(__file__).parent / "device_lookup.json"
_ANDROID_LOOKUP_PATH = Path(__file__).parent / "android_device_lookup.json"


@lru_cache(maxsize=1)
def _load_ios_lookup() -> dict[str, str]:
    """
    Load device_lookup.json and return a productType -> marketing name map.
    Cached after first call.  Returns empty dict on any read/parse error.
    """
    try:
        data = json.loads(_IOS_LOOKUP_PATH.read_text(encoding="utf-8"))
        devices: dict = data.get("devices", {})
        return {k: v["name"] for k, v in devices.items() if "name" in v}
    except Exception as exc:
        logger.warning("Could not load device_lookup.json: %s", exc)
        return {}


@lru_cache(maxsize=1)
def _load_android_lookup() -> dict[str, str]:
    """
    Load android_device_lookup.json and return a model -> marketing name map.
    Source: Wondershare dr.fone repair.mapping.db (2004 Samsung models).
    Cached after first call.  Returns empty dict on any read/parse error.
    """
    try:
        data = json.loads(_ANDROID_LOOKUP_PATH.read_text(encoding="utf-8"))
        return data.get("devices", {})
    except Exception as exc:
        logger.warning("Could not load android_device_lookup.json: %s", exc)
        return {}


def resolve_ios_model(product_type: str) -> str:
    """
    Return the marketing name for an iOS ProductType string.

    "iPhone15,3" -> "iPhone 14 Pro Max"
    "UnknownX,Y" -> "UnknownX,Y"  (returned unchanged)
    """
    return _load_ios_lookup().get(product_type, product_type)


def refresh_caches() -> None:
    """
    Clear the in-memory lookup caches so the next call to resolve_ios_model()
    or resolve_android_name() reloads from the updated JSON files on disk.
    Call this after the background enrichment job finishes writing new data.
    """
    _load_ios_lookup.cache_clear()
    _load_android_lookup.cache_clear()


def resolve_android_name(model: str, brand: str = "") -> str:
    """
    Return a display-friendly name for an Android device.

    Lookup order:
    1. android_device_lookup.json (2004 Samsung models from dr.fone)
       "SM-G991B" -> "Galaxy S21 5G"
    2. Brand prefix fallback: title-case brand + model
       "SM-X123", brand="samsung" -> "Samsung SM-X123"
    3. Raw model if no brand available.

    Examples:
        model="SM-G991B",    brand="samsung" -> "Samsung Galaxy S21 5G"
        model="SM-X999",     brand="samsung" -> "Samsung SM-X999"
        model="Pixel 7 Pro", brand="google"  -> "Google Pixel 7 Pro"
        model="Pixel 7 Pro", brand=""        -> "Pixel 7 Pro"
    """
    lookup = _load_android_lookup()
    brand_title = brand.strip().title() if brand else ""

    if model in lookup:
        marketing_name = lookup[model]
        # Prepend brand unless the marketing name already starts with it
        if brand_title and not marketing_name.lower().startswith(brand.strip().lower()):
            return f"{brand_title} {marketing_name}"
        return marketing_name

    # Not in lookup — fall back to brand prefix
    if not brand_title:
        return model
    if model.lower().startswith(brand.strip().lower()):
        return model
    return f"{brand_title} {model}"
