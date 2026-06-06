"""
ios_backup_domains.py

Centralised registry of iOS backup domain strings, relative paths, and
third-party app bundle IDs used by the PhoneTransfer extraction/injection
pipeline.

Previously every extractor hardcoded its own domain + path pair.  This
module provides a single source of truth, informed by the Wondershare
Dr.Fone iOS device interface DLL analysis:

- **HomeDomain**: system databases (SMS, Contacts, Notes, Calendar, etc.)
- **MediaDomain**: photos metadata, SMS attachments, ringtones, recordings
- **CameraRollDomain**: DCIM photos/videos + PhotoData
- **AppDomain-<bundle>**: per-app sandbox data
- **AppDomainGroup-<group>**: shared app-group containers
- **WirelessDomain**: legacy call history, carrier settings
- **SystemPreferencesDomain**: device preferences, SpringBoard settings
- **DatabaseDomain**: system-level databases

Usage
-----
    from core.ios_backup_domains import DOMAINS, SOCIAL_APPS, domain_for

    domain, path = DOMAINS["sms"]
    # ("HomeDomain", "Library/SMS/sms.db")

    bundle_id = SOCIAL_APPS["whatsapp"]["bundle_id"]
    # "net.whatsapp.WhatsApp"
"""

from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Domain + path pairs for built-in iOS data categories
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DomainEntry:
    """A single iOS backup domain + relative path pair."""
    domain: str
    relative_path: str
    device_path: str = ""  # full on-device path (for AFC2/jailbroken access)
    description: str = ""


# Key: category name used by PhoneTransfer's pipeline
DOMAINS: dict[str, DomainEntry] = {

    # ── Messages ──────────────────────────────────────────────────────
    "sms": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/SMS/sms.db",
        device_path="/var/mobile/Library/SMS/sms.db",
        description="SMS, MMS, and iMessage records",
    ),
    "sms_attachments": DomainEntry(
        domain="MediaDomain",
        relative_path="Library/SMS/Attachments",
        device_path="/var/mobile/Library/SMS/Attachments",
        description="SMS/MMS/iMessage media attachments directory",
    ),

    # ── Contacts ──────────────────────────────────────────────────────
    "contacts": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/AddressBook/AddressBook.sqlitedb",
        device_path="/var/mobile/Library/AddressBook/AddressBook.sqlitedb",
        description="All contacts (ABPerson + ABMultiValue)",
    ),
    "contact_images": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/AddressBook/AddressBookImages.sqlitedb",
        device_path="/var/mobile/Library/AddressBook/AddressBookImages.sqlitedb",
        description="Contact photo thumbnails",
    ),

    # ── Call History ──────────────────────────────────────────────────
    "calls": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/CallHistoryDB/CallHistory.storedata",
        device_path="/var/mobile/Library/CallHistoryDB/CallHistory.storedata",
        description="Call history — iOS 8+ Core Data store (ZCALLRECORD)",
    ),
    "calls_legacy": DomainEntry(
        domain="WirelessDomain",
        relative_path="Library/CallHistory/call_history.db",
        device_path="/var/wireless/Library/CallHistory/call_history.db",
        description="Call history — legacy pre-iOS 8 format",
    ),

    # ── Calendar ──────────────────────────────────────────────────────
    "calendar": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/Calendars/Calendar.sqlitedb",
        device_path="/var/mobile/Library/Calendars/Calendar.sqlitedb",
        description="Calendar events (Core Data: ZCALCALENDARITEM)",
    ),

    # ── Notes ─────────────────────────────────────────────────────────
    "notes": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/Notes/notes.sqlite",
        device_path="/var/mobile/Library/Notes/notes.sqlite",
        description="Apple Notes (ZNOTE / ZNOTEBODY)",
    ),

    # ── Reminders ─────────────────────────────────────────────────────
    "reminders": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/Reminders/Container_v1/Stores",
        device_path="/var/mobile/Library/Reminders/Container_v1/Stores",
        description="Reminders (per-store SQLite files)",
    ),

    # ── Safari ────────────────────────────────────────────────────────
    "bookmarks": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/Safari/Bookmarks.db",
        device_path="/var/mobile/Library/Safari/Bookmarks.db",
        description="Safari bookmarks",
    ),
    "browser_history": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/Safari/History.db",
        device_path="/var/mobile/Library/Safari/History.db",
        description="Safari browsing history",
    ),

    # ── Voicemail ─────────────────────────────────────────────────────
    "voicemail": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/Voicemail/voicemail.db",
        device_path="/var/mobile/Library/Voicemail/voicemail.db",
        description="Voicemail metadata",
    ),
    "voicemail_audio": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/Voicemail",
        device_path="/var/mobile/Library/Voicemail",
        description="Voicemail audio files (.amr)",
    ),

    # ── Health ────────────────────────────────────────────────────────
    "health": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/Health/healthdb.sqlite",
        device_path="/var/mobile/Library/Health/healthdb.sqlite",
        description="Apple Health data (encrypted backups only)",
    ),

    # ── Photos & Camera ──────────────────────────────────────────────
    "photos_db": DomainEntry(
        domain="CameraRollDomain",
        relative_path="Media/PhotoData/Photos.sqlite",
        device_path="/var/mobile/Media/PhotoData/Photos.sqlite",
        description="Photos library metadata (ZGENERICASSET)",
    ),
    "photos_dcim": DomainEntry(
        domain="CameraRollDomain",
        relative_path="Media/DCIM",
        device_path="/var/mobile/Media/DCIM",
        description="Camera roll photos and videos",
    ),

    # ── Ringtones ─────────────────────────────────────────────────────
    "ringtones": DomainEntry(
        domain="MediaDomain",
        relative_path="iTunes_Control/Ringtones",
        device_path="/var/mobile/Media/iTunes_Control/Ringtones",
        description="Custom ringtones (.m4r files)",
    ),

    # ── Voice Memos ───────────────────────────────────────────────────
    "voice_memos": DomainEntry(
        domain="MediaDomain",
        relative_path="Recordings",
        device_path="/var/mobile/Media/Recordings",
        description="Voice Memos recordings",
    ),

    # ── Wallpaper (SpringBoard) ───────────────────────────────────────
    "wallpaper": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/SpringBoard",
        device_path="/var/mobile/Library/SpringBoard",
        description="SpringBoard wallpaper settings and cached images",
    ),
    "wallpaper_lockscreen": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/SpringBoard/LockBackground.cpbitmap",
        device_path="/var/mobile/Library/SpringBoard/LockBackground.cpbitmap",
        description="Lock screen wallpaper (cpbitmap format)",
    ),
    "wallpaper_homescreen": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/SpringBoard/HomeBackground.cpbitmap",
        device_path="/var/mobile/Library/SpringBoard/HomeBackground.cpbitmap",
        description="Home screen wallpaper (cpbitmap format)",
    ),
    "wallpaper_original_lock": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/SpringBoard/OriginalLockBackground.jpg",
        device_path="/var/mobile/Library/SpringBoard/OriginalLockBackground.jpg",
        description="Original lock screen wallpaper (JPEG)",
    ),
    "wallpaper_original_home": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/SpringBoard/OriginalHomeBackground.jpg",
        device_path="/var/mobile/Library/SpringBoard/OriginalHomeBackground.jpg",
        description="Original home screen wallpaper (JPEG)",
    ),

    # ── System Preferences ────────────────────────────────────────────
    "global_preferences": DomainEntry(
        domain="SystemPreferencesDomain",
        relative_path="Library/Preferences/.GlobalPreferences.plist",
        device_path="/var/mobile/Library/Preferences/.GlobalPreferences.plist",
        description="Global device preferences (language, timezone, etc.)",
    ),

    # ── Blocked Contacts ──────────────────────────────────────────────
    "blocked": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/CallHistoryDB/CallHistory.storedata",
        device_path="/var/mobile/Library/CallHistoryDB/CallHistory.storedata",
        description="Blocked contacts (stored in CallHistory Core Data store)",
    ),

    # ── Alarms ────────────────────────────────────────────────────────
    "alarms": DomainEntry(
        domain="HomeDomain",
        relative_path="Library/Preferences/com.apple.mobiletimerd.plist",
        device_path="/var/mobile/Library/Preferences/com.apple.mobiletimerd.plist",
        description="Alarm settings (plist format)",
    ),
}


# ---------------------------------------------------------------------------
# Third-party social / messaging app bundle IDs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SocialAppEntry:
    """Registry entry for a third-party messaging app."""
    bundle_id: str
    display_name: str
    db_relative_path: str          # main database within app sandbox
    db_schema_table: str = ""      # primary message table name
    contacts_path: str = ""        # contacts/roster database (optional)
    media_path: str = ""           # media attachments directory (optional)
    app_group: str = ""            # shared app-group container ID (optional)


SOCIAL_APPS: dict[str, SocialAppEntry] = {

    "whatsapp": SocialAppEntry(
        bundle_id="net.whatsapp.WhatsApp",
        display_name="WhatsApp",
        db_relative_path="Documents/ChatStorage.sqlite",
        db_schema_table="ZWAMESSAGE",
        contacts_path="Documents/ContactsV2.sqlite",
        media_path="Library/Media",
        app_group="group.net.whatsapp.WhatsApp.shared",
    ),

    "viber": SocialAppEntry(
        bundle_id="com.viber",
        display_name="Viber",
        db_relative_path="Documents/Contacts.data",
        db_schema_table="ZVIBERMESSAGE",
        contacts_path="Documents/Contacts.data",
        media_path="Library/Application Support/Attachments",
    ),

    "line": SocialAppEntry(
        bundle_id="jp.naver.line",
        display_name="LINE",
        db_relative_path="Library/Application Support/PrivateStore/P_/Message/Line.sqlite",
        db_schema_table="ZMESSAGE",
        contacts_path="Library/Application Support/PrivateStore/P_/Contact/Contact.sqlite",
        media_path="Library/Application Support/PrivateStore/P_/Message/Attachments",
    ),

    "kik": SocialAppEntry(
        bundle_id="com.kik.chat",
        display_name="Kik",
        db_relative_path="Documents/kik.sqlite",
        db_schema_table="ZKIKMESSAGE",
        media_path="Documents/content-manager",
    ),

    "wechat": SocialAppEntry(
        bundle_id="com.tencent.xin",
        display_name="WeChat",
        db_relative_path="Documents/<hash>/DB/MM.sqlite",
        db_schema_table="Chat_<hash>",
        media_path="Documents/<hash>/Video",
        app_group="group.com.tencent.xin",
    ),

    "qq": SocialAppEntry(
        bundle_id="com.tencent.mqq",
        display_name="QQ",
        db_relative_path="Documents/<qq_number>/msg_0.db",
        db_schema_table="tb_message_<hash>",
        media_path="Documents/<qq_number>/video",
    ),

    "telegram": SocialAppEntry(
        bundle_id="ph.telegra.Telegraph",
        display_name="Telegram",
        db_relative_path="Documents/accounts-metadata",
        db_schema_table="",  # binary format, not SQLite
        app_group="group.ph.telegra.Telegraph",
    ),

    "signal": SocialAppEntry(
        bundle_id="org.whispersystems.signal",
        display_name="Signal",
        db_relative_path="Documents/grdb/signal.sqlite",
        db_schema_table="model_TSInteraction",
        app_group="group.org.whispersystems.signal",
    ),

    "facebook_messenger": SocialAppEntry(
        bundle_id="com.facebook.Messenger",
        display_name="Messenger",
        db_relative_path="Library/Caches/com.facebook.orca/msys_database_v2",
        db_schema_table="messages",
        app_group="group.com.facebook.Messenger",
    ),

    "instagram": SocialAppEntry(
        bundle_id="com.burbn.instagram",
        display_name="Instagram",
        db_relative_path="Library/Application Support/DirectMessages/direct.sqlite",
        db_schema_table="messages",
    ),

    "snapchat": SocialAppEntry(
        bundle_id="com.toyopagroup.picaboo",
        display_name="Snapchat",
        db_relative_path="Documents/user_scoped/<uuid>/arroyo/arroyo.db",
        db_schema_table="conversation_message",
    ),

    "discord": SocialAppEntry(
        bundle_id="com.hammerandchisel.discord",
        display_name="Discord",
        db_relative_path="",  # Discord uses server-side storage primarily
        db_schema_table="",
    ),
}


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def domain_for(category: str) -> DomainEntry | None:
    """
    Look up the domain entry for a given PhoneTransfer category.

    Returns None if the category is not in the registry.
    """
    return DOMAINS.get(category)


def app_domain(bundle_id: str) -> str:
    """
    Build the iOS backup domain string for a third-party app.

    Example: ``app_domain("net.whatsapp.WhatsApp")``
    returns ``"AppDomain-net.whatsapp.WhatsApp"``.
    """
    return f"AppDomain-{bundle_id}"


def app_group_domain(group_id: str) -> str:
    """
    Build the iOS backup domain string for a shared app-group container.

    Example: ``app_group_domain("group.net.whatsapp.WhatsApp.shared")``
    returns ``"AppDomainGroup-group.net.whatsapp.WhatsApp.shared"``.
    """
    return f"AppDomainGroup-{group_id}"


def social_app_domain(app_key: str) -> str | None:
    """
    Get the full iOS backup domain string for a social app by key.

    Returns None if the app key is not found.

    Example: ``social_app_domain("whatsapp")``
    returns ``"AppDomain-net.whatsapp.WhatsApp"``.
    """
    entry = SOCIAL_APPS.get(app_key)
    if entry is None:
        return None
    return app_domain(entry.bundle_id)


def list_social_app_domains() -> dict[str, str]:
    """
    Return a mapping of social app keys to their iOS backup domain strings.

    Useful for scanning a backup's Manifest.db for installed apps.
    """
    return {key: app_domain(entry.bundle_id) for key, entry in SOCIAL_APPS.items()}
