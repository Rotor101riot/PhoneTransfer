"""
core/vault_format.py

Single source of truth for the PhoneTransfer Neutral Transfer Format (NTF).

Every extractor, injector, vault writer, vault reader, and companion APK
communicates through this shared schema.  Nothing outside this module should
hard-code category names, filenames, ZIP layout paths, protocol constants, or
manifest field names.

Vault ZIP layout (version 1.0)
-------------------------------
  manifest.json              Required — archive metadata and item counts
  contacts.json              list[Contact]
  sms.json                   list[Message]  (SMS/MMS)
  calls.json                 list[CallRecord]
  calendar.json              list[CalendarEvent]
  notes.json                 list[Note]
  alarms.json                list[Alarm]
  reminders.json             list[Reminder]
  bookmarks.json             list[Bookmark]
  blocked.json               list[BlockedNumber]
  health.json                list[HealthSample]
  browser.json               list[BrowserHistoryItem]
  whatsapp.json              list[Message]  (WhatsApp conversations)
  telegram.json              list[Message]  (Telegram conversations)
  photos.json                list[MediaFile]
  videos.json                list[MediaFile]
  ringtones.json             list[MediaFile]
  voice_memos.json           list[MediaFile]
  wallpaper.json             list[MediaFile]
  media/photos/              photo binaries referenced by photos.json
  media/videos/              video binaries referenced by videos.json
  media/ringtones/           ringtone binaries
  media/voice_memos/         voice memo binaries
  media/wallpaper/           wallpaper binaries
  media/attachments/         binaries for message/WhatsApp/Telegram attachments

Manifest schema (manifest.json)
---------------------------------
Required fields:
  vault_version   str   Format version string; currently "1.0"
  created_at      str   ISO-8601 UTC timestamp of backup creation
  source_platform str   "ios" or "android"
  item_counts     dict  {category: int} — items written per category

Optional fields:
  source_model    str   Device model identifier
  source_name     str   User-visible device name
  source_os       str   OS version string
  source_udid     str   Device UDID / serial

Companion TCP frame protocol
-----------------------------
  Port:            7337
  Service type:    _phonetransfer._tcp.local.  (mDNS / Zeroconf)
  Max frame size:  64 MiB
  Length prefix:   4-byte little-endian uint32 before each JSON body
  Encoding:        UTF-8 JSON
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Format version
# ---------------------------------------------------------------------------

VAULT_FORMAT_VERSION = "1.0"
MANIFEST_FILENAME    = "manifest.json"

# ---------------------------------------------------------------------------
# Category → JSON filename inside the vault ZIP
# ---------------------------------------------------------------------------

CATEGORY_FILENAMES: dict[str, str] = {
    "contacts":    "contacts.json",
    "sms":         "sms.json",
    "calls":       "calls.json",
    "calendar":    "calendar.json",
    "notes":       "notes.json",
    "alarms":      "alarms.json",
    "reminders":   "reminders.json",
    "bookmarks":   "bookmarks.json",
    "blocked":     "blocked.json",
    "health":      "health.json",
    "browser":     "browser.json",
    "whatsapp":    "whatsapp.json",
    "telegram":    "telegram.json",
    "photos":      "photos.json",
    "videos":      "videos.json",
    "ringtones":   "ringtones.json",
    "voice_memos": "voice_memos.json",
    "wallpaper":   "wallpaper.json",
    "apps":        "apps.json",
}

# ---------------------------------------------------------------------------
# Media categories → ZIP subdirectory for their binary files
# ---------------------------------------------------------------------------

MEDIA_SUBDIRS: dict[str, str] = {
    "photos":      "media/photos",
    "videos":      "media/videos",
    "ringtones":   "media/ringtones",
    "voice_memos": "media/voice_memos",
    "wallpaper":   "media/wallpaper",
}

# Categories whose Message items may carry file attachments written to
# media/attachments/ inside the ZIP.
ATTACHMENT_CATEGORIES: frozenset[str] = frozenset({"sms", "whatsapp", "telegram"})

# ---------------------------------------------------------------------------
# Companion TCP protocol constants
# ---------------------------------------------------------------------------

COMPANION_PORT        = 7337
COMPANION_SERVICE_TYPE = "_phonetransfer._tcp.local."
MAX_FRAME_BYTES       = 64 * 1024 * 1024   # 64 MiB
FRAME_LENGTH_FORMAT   = "<I"               # 4-byte little-endian uint32

# ---------------------------------------------------------------------------
# Manifest schema helpers
# ---------------------------------------------------------------------------

REQUIRED_MANIFEST_FIELDS: frozenset[str] = frozenset({
    "vault_version",
    "created_at",
    "source_platform",
    "item_counts",
})

OPTIONAL_MANIFEST_FIELDS: frozenset[str] = frozenset({
    "source_model",
    "source_name",
    "source_os",
    "source_udid",
    "total_items",
})


def validate_manifest(manifest: dict) -> list[str]:
    """
    Validate a parsed manifest dict against the NTF schema.

    Parameters
    ----------
    manifest:
        Dict loaded from ``manifest.json`` inside a vault ZIP.

    Returns
    -------
    list[str]
        Validation error messages.  An empty list means the manifest is valid.
    """
    errors: list[str] = []

    for field in REQUIRED_MANIFEST_FIELDS:
        if field not in manifest:
            errors.append(f"Missing required manifest field: '{field}'")

    version = manifest.get("vault_version")
    if version and version != VAULT_FORMAT_VERSION:
        errors.append(
            f"Unsupported vault version: '{version}' "
            f"(expected '{VAULT_FORMAT_VERSION}')"
        )

    platform = manifest.get("source_platform")
    if platform and platform not in ("ios", "android"):
        errors.append(
            f"Invalid source_platform: '{platform}' (must be 'ios' or 'android')"
        )

    counts = manifest.get("item_counts")
    if counts is not None and not isinstance(counts, dict):
        errors.append("'item_counts' must be a JSON object (dict)")

    return errors


def category_filename(category: str) -> str:
    """Return the in-ZIP JSON filename for *category*, with a safe fallback."""
    return CATEGORY_FILENAMES.get(category, f"{category}.json")


def media_subdir(category: str) -> str:
    """Return the in-ZIP binary subdirectory for a media category, or ''."""
    return MEDIA_SUBDIRS.get(category, "")
