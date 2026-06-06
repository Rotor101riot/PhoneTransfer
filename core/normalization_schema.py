"""
normalization_schema.py

Defines the canonical intermediate data format used throughout the
PhoneTransfer pipeline.  Every platform-specific extractor produces these
dataclasses; every platform-specific injector consumes them.  Nothing outside
this module should define its own data containers for the types listed here.

Design decisions:
- Pure stdlib — no external dependencies.
- Defaults lean toward "empty / None" so partial data never causes crashes.
- Literal types constrain string enums to valid values at type-check time.
- datetime objects are always timezone-aware where possible; callers that
  receive naive datetimes should attach UTC before storing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal


# ---------------------------------------------------------------------------
# Device metadata
# ---------------------------------------------------------------------------

@dataclass
class DeviceInfo:
    """Describes a single connected device (iOS or Android)."""
    udid: str
    platform: Literal["ios", "android"]
    model: str          # e.g. "iPhone15,2" or "SM-G991B"
    name: str           # user-visible device name
    os_version: str     # e.g. "17.4.1" or "14"
    is_jailbroken: bool
    is_rooted: bool
    serial: str         # adb serial or iOS UDID (same as udid for iOS)
    brand: str = ""     # Android: ro.product.brand (e.g. "samsung", "google")
    transport: str = "usb"       # "usb" | "wifi"
    wifi_host: str | None = None  # device IP address when transport == "wifi"
    wifi_port: int = 7337         # companion TCP port when transport == "wifi"


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

@dataclass
class Contact:
    first_name: str | None = None
    last_name: str | None = None
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    organization: str | None = None
    note: str | None = None
    raw_vcard: str | None = None    # original vCard string if available


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

@dataclass
class MessageAttachment:
    filename: str
    mime_type: str
    data: bytes | None = None           # None if not yet loaded into memory
    local_path: Path | None = None      # path if staged to disk


@dataclass
class Message:
    platform_id: str                    # original DB row id as string
    sender: str                         # phone number or "self"
    recipient: str
    body: str
    timestamp: datetime
    is_sent: bool
    attachments: list[MessageAttachment] = field(default_factory=list)
    service: Literal["sms", "mms", "imessage", "rcs"] = "sms"
    read: bool = True
    # Android SMS type: 1=inbox, 2=sent, 3=draft, 4=outbox, 5=failed, 6=queued
    sms_type: int = 0                   # 0 = auto-detect from is_sent
    thread_id: int = 0                  # 0 = let Android recalculate
    status: int = -1                    # Android SMS status (-1 = none)
    # MMS-specific fields (populated for service="mms")
    subject: str = ""                   # MMS subject line
    from_addresses: list[str] = field(default_factory=list)  # all FROM addresses (group MMS)
    to_addresses: list[str] = field(default_factory=list)    # all TO addresses (group MMS)
    cc_addresses: list[str] = field(default_factory=list)    # CC addresses (rare)


# ---------------------------------------------------------------------------
# Call log
# ---------------------------------------------------------------------------

@dataclass
class CallRecord:
    number: str
    timestamp: datetime
    duration_seconds: int
    call_type: Literal["incoming", "outgoing", "missed"]
    name: str | None = None


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

@dataclass
class CalendarEvent:
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    uid: str | None = None
    location: str | None = None
    notes: str | None = None
    recurrence_rule: str | None = None  # RFC 5545 RRULE string


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

@dataclass
class Note:
    title: str
    body: str
    created: datetime | None = None
    modified: datetime | None = None
    folder: str | None = None


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------

@dataclass
class MediaFile:
    filename: str
    mime_type: str
    local_path: Path
    created: datetime | None = None
    album: str | None = None
    latitude: float | None = None
    longitude: float | None = None


# ---------------------------------------------------------------------------
# Blocked numbers
# ---------------------------------------------------------------------------

@dataclass
class BlockedNumber:
    number: str
    name: str | None = None


# ---------------------------------------------------------------------------
# Alarms
# ---------------------------------------------------------------------------

@dataclass
class Alarm:
    hour: int                           # 0-23
    minute: int                         # 0-59
    label: str = ""
    enabled: bool = True
    repeat_days: list[int] = field(default_factory=list)  # 0=Mon..6=Sun (ISO)
    sound: str | None = None            # ringtone name / filename


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

@dataclass
class Reminder:
    title: str
    due: datetime | None = None
    notes: str | None = None
    completed: bool = False
    list_name: str | None = None
    uid: str | None = None
    priority: int = 0                   # 0=none 1=high 5=medium 9=low (RFC 5545)


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------

@dataclass
class Bookmark:
    title: str
    url: str
    folder: str | None = None
    added: datetime | None = None


# ---------------------------------------------------------------------------
# Voicemail
# ---------------------------------------------------------------------------

@dataclass
class Voicemail:
    """A single voicemail entry with its audio payload.

    ``audio_bytes`` is the raw container (typically .amr for iOS, .m4a /
    .3gp on Android).  Keep it inline rather than out-of-band because a
    voicemail is useless without its audio — splitting would force every
    extractor and injector to re-correlate metadata with audio on disk.
    """
    sender: str
    received: datetime
    duration_seconds: int
    audio_bytes: bytes
    token: str | None = None
    audio_mime: str = "audio/amr"


# ---------------------------------------------------------------------------
# Health & Fitness
# ---------------------------------------------------------------------------

@dataclass
class HealthRecord:
    """
    A single health or fitness measurement.

    category examples: "steps", "heart_rate", "sleep", "workout",
    "calories", "weight", "height", "blood_pressure_systolic",
    "blood_pressure_diastolic", "blood_glucose", "oxygen_saturation",
    "body_temperature", "distance", "floors_climbed", "resting_heart_rate"
    """
    category: str
    value: float
    unit: str               # "count", "bpm", "min", "kg", "kcal", "mmHg", "mg/dL", "%", "°C", "m"
    start: datetime
    end: datetime | None = None
    source_name: str | None = None   # app or device that recorded it
    notes: str | None = None


# ---------------------------------------------------------------------------
# Browser history
# ---------------------------------------------------------------------------

@dataclass
class BrowserHistoryEntry:
    url: str
    title: str = ""
    visited: datetime = field(default_factory=lambda: datetime.utcnow())
    visit_count: int = 1
    browser: str = "unknown"   # "chrome", "safari", "firefox", "edge", "samsung"


# ---------------------------------------------------------------------------
# Contact groups
# ---------------------------------------------------------------------------

@dataclass
class ContactGroup:
    title: str
    group_id: int | None = None
    account_name: str | None = None
    account_type: str | None = None
    visible: bool = True
    notes: str | None = None
    member_count: int = 0
    # Members named as "First Last" so the iOS injector can resolve them
    # against ABPerson.First / ABPerson.Last after the contacts injector
    # has run.  Empty for groups whose membership we don't know yet.
    member_names: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------

@dataclass
class ClipboardItem:
    text: str
    mime_type: str | None = None


# ---------------------------------------------------------------------------
# Installed apps
# ---------------------------------------------------------------------------

@dataclass
class InstalledApp:
    package_name: str
    app_name: str = ""
    version_name: str | None = None
    version_code: int | None = None
    apk_size: int = 0               # bytes
    install_time: int | None = None  # epoch milliseconds
    update_time: int | None = None   # epoch milliseconds
    is_system: bool = False
    apk_local_path: Path | None = None  # local path to backed-up APK (if available)


# ---------------------------------------------------------------------------
# Mail accounts
# ---------------------------------------------------------------------------

@dataclass
class MailAccount:
    """Metadata for a configured email account (no passwords/tokens)."""
    email: str
    account_type: str           # e.g. "com.google", "com.microsoft.exchange"
    display_name: str = ""
    server_host: str | None = None   # IMAP/POP host if available
    server_port: int | None = None


# ---------------------------------------------------------------------------
# Transfer manifest — top-level container
# ---------------------------------------------------------------------------

@dataclass
class TransferManifest:
    """
    Aggregates all data to be transferred in a single object.
    Produced after extraction is complete; passed to injectors.
    """
    source: DeviceInfo
    destination: DeviceInfo
    created_at: datetime = field(default_factory=datetime.utcnow)
    transfer_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    contacts: list[Contact] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    calls: list[CallRecord] = field(default_factory=list)
    events: list[CalendarEvent] = field(default_factory=list)
    reminders: list[Reminder] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    media: list[MediaFile] = field(default_factory=list)
    blocked: list[BlockedNumber] = field(default_factory=list)
    alarms: list[Alarm] = field(default_factory=list)
    bookmarks: list[Bookmark] = field(default_factory=list)
    contact_groups: list[ContactGroup] = field(default_factory=list)
    clipboard: list[ClipboardItem] = field(default_factory=list)
    installed_apps: list[InstalledApp] = field(default_factory=list)
    browser_history: list[BrowserHistoryEntry] = field(default_factory=list)
