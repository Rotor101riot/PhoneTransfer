"""
core/wifi_android_extractor.py

Extract and inject Android data over Wi-Fi via the companion APK,
without requiring a USB cable.

The companion APK runs a TCP socket server on port 7337 and advertises
itself via mDNS (_phonetransfer._tcp).  This module wraps a connected
WifiCompanionSession and exposes the same extract/inject interface that
the pipeline and vault manager expect from ADB-based modules, so the
rest of the codebase is transport-agnostic.

Usage
-----
    from core.wifi_discovery import WifiCompanionSession, discover_companions
    from core.wifi_android_extractor import WifiAndroidExtractor
    from pathlib import Path

    devices = discover_companions(timeout=5.0)
    with WifiCompanionSession(devices[0]) as session:
        ex = WifiAndroidExtractor(session)
        contacts = ex.extract("contacts", Path("/tmp/staging"))
        ex.inject("contacts", contacts, Path("/tmp/staging"))

Category support
----------------
Categories backed by the companion APK handlers (full support):
    contacts, sms, calls, calendar, notes, alarms, reminders,
    bookmarks, blocked, whatsapp, telegram, photos, videos,
    ringtones, voice_memos, wallpaper

Categories not yet supported over Wi-Fi (returns []):
    health, browser, signal
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.wifi_discovery import WifiCompanionSession

from core.normalization_schema import (
    Alarm, BlockedNumber, Bookmark, BrowserHistoryEntry, CalendarEvent,
    CallRecord, ClipboardItem, Contact, ContactGroup, InstalledApp,
    MediaFile, Message, MessageAttachment, Note, Reminder,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Categories the companion APK can handle over Wi-Fi
# ---------------------------------------------------------------------------

_WIFI_SUPPORTED: frozenset[str] = frozenset({
    "contacts", "contact_groups", "sms", "calls", "calendar", "notes",
    "alarms", "reminders", "bookmarks", "blocked",
    "browser_history", "clipboard", "installed_apps",
    "whatsapp", "telegram",
    "photos", "videos", "ringtones", "voice_memos", "wallpaper",
})


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class WifiAndroidExtractor:
    """
    Routes extract/inject calls through a live WifiCompanionSession.

    Parameters
    ----------
    session:
        A *connected* WifiCompanionSession instance.
    """

    def __init__(self, session: "WifiCompanionSession") -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Public API (mirrors the ADB extractor/injector signature)
    # ------------------------------------------------------------------

    def extract(self, category: str, staging_dir: Path) -> list:
        """
        Ask the companion APK to extract *category* and return a list of
        normalised dataclass instances.

        Parameters
        ----------
        category:
            One of the supported category strings.
        staging_dir:
            Local staging directory.  Media files are written here.

        Returns
        -------
        List of normalised objects (Contact, Message, CallRecord, etc.).
        Empty list if the category is unsupported or extraction fails.
        """
        if category not in _WIFI_SUPPORTED:
            logger.warning(
                "wifi_extractor: category '%s' is not supported over Wi-Fi", category
            )
            return []

        try:
            response = self._session.extract(category)
        except Exception as exc:
            logger.error("wifi_extractor: extract '%s' failed: %s", category, exc)
            return []

        if response.get("status") != "ok":
            logger.warning(
                "wifi_extractor: APK returned status '%s' for '%s': %s",
                response.get("status"), category, response.get("error"),
            )
            return []

        raw_items: list[dict] = response.get("items", [])
        if not raw_items:
            return []

        # Pull any media files from the companion into staging_dir
        if category in {"photos", "videos", "ringtones", "voice_memos", "wallpaper"}:
            return self._receive_media(category, raw_items, staging_dir)

        factory = _FACTORY.get(category)
        if factory is None:
            logger.warning("wifi_extractor: no factory for category '%s'", category)
            return []

        result = []
        for raw in raw_items:
            try:
                result.append(factory(raw))
            except Exception as exc:
                logger.debug("wifi_extractor: skipping malformed item in '%s': %s", category, exc)
        return result

    def inject(self, category: str, items: list, staging_dir: Path) -> int:
        """
        Send *items* to the companion APK for injection into the device.

        Parameters
        ----------
        category:
            Data category string.
        items:
            List of normalised dataclass instances.
        staging_dir:
            Local staging directory.  Media files are pushed from here.

        Returns
        -------
        Number of items the APK reports successfully injected.
        """
        if not items:
            return 0

        if category not in _WIFI_SUPPORTED:
            logger.warning(
                "wifi_extractor: inject '%s' is not supported over Wi-Fi", category
            )
            return 0

        from dataclasses import asdict
        serialised = [asdict(item) for item in items]

        # For media categories, push binary files before the inject command
        if category in {"photos", "videos", "ringtones", "voice_memos", "wallpaper"}:
            self._push_media(category, serialised, staging_dir)

        try:
            response = self._session.inject(category, serialised)
        except Exception as exc:
            logger.error("wifi_extractor: inject '%s' failed: %s", category, exc)
            return 0

        if response.get("status") != "ok":
            logger.warning(
                "wifi_extractor: APK inject '%s' returned '%s': %s",
                category, response.get("status"), response.get("error"),
            )
            return 0

        return int(response.get("count", len(items)))

    # ------------------------------------------------------------------
    # Media transfer helpers
    # ------------------------------------------------------------------

    def _receive_media(
        self,
        category: str,
        raw_items: list[dict],
        staging_dir: Path,
    ) -> list[MediaFile]:
        """
        For each item in raw_items, pull the binary from the companion and
        write it to staging_dir, then return MediaFile instances.
        """
        staging_dir.mkdir(parents=True, exist_ok=True)
        result: list[MediaFile] = []

        for raw in raw_items:
            remote_path = raw.get("remote_path") or raw.get("local_path") or ""
            filename    = raw.get("filename") or os.path.basename(remote_path) or "file"
            local_path  = staging_dir / filename

            try:
                pull_resp = self._session.send_recv({
                    "cmd":  "pull_file",
                    "path": remote_path,
                })
                if pull_resp.get("status") == "ok":
                    # Companion sends binary data as base64 in "data" field
                    import base64
                    try:
                        data = base64.b64decode(pull_resp.get("data", ""))
                    except Exception as b64_exc:
                        logger.warning(
                            "wifi_extractor: base64 decode failed for '%s': %s",
                            remote_path, b64_exc,
                        )
                        local_path = None
                        continue
                    local_path.write_bytes(data)
                else:
                    local_path = None
            except Exception as exc:
                logger.debug(
                    "wifi_extractor: pull_file '%s' failed: %s", remote_path, exc
                )
                local_path = None

            result.append(MediaFile(
                filename  = filename,
                local_path= local_path,
                mime_type = raw.get("mime_type", ""),
                created   = _dt(raw.get("created_at") or raw.get("created")),
                album     = raw.get("album"),
            ))

        return result

    def _push_media(
        self,
        category: str,
        serialised: list[dict],
        staging_dir: Path,
    ) -> None:
        """
        Push each local_path file to the companion before the inject command.
        Updates the serialised dict's local_path to the remote path returned
        by the companion so the inject handler can reference it.
        """
        import base64

        for item in serialised:
            local_path = item.get("local_path")
            if not local_path:
                continue
            p = Path(str(local_path))
            if not p.exists():
                continue
            try:
                data = p.read_bytes()
                push_resp = self._session.send_recv({
                    "cmd":      "push_file",
                    "filename": p.name,
                    "category": category,
                    "data":     base64.b64encode(data).decode(),
                })
                if push_resp.get("status") == "ok":
                    item["remote_path"] = push_resp.get("path", "")
            except Exception as exc:
                logger.debug(
                    "wifi_extractor: push_file '%s' failed: %s", p.name, exc
                )


# ---------------------------------------------------------------------------
# Per-category factories: dict → dataclass
# ---------------------------------------------------------------------------

def _dt(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _contact(d: dict) -> Contact:
    return Contact(
        first_name   = d.get("first_name"),
        last_name    = d.get("last_name"),
        phones       = d.get("phones") or [],
        emails       = d.get("emails") or [],
        organization = d.get("organization"),
        note         = d.get("note"),
        raw_vcard    = d.get("raw_vcard"),
    )


def _message(d: dict) -> Message:
    attachments = [
        MessageAttachment(
            filename  = a.get("filename", "") or a.get("name", ""),
            mime_type = a.get("mime_type", "") or a.get("content_type", ""),
            local_path= Path(a["local_path"]) if a.get("local_path") else None,
        )
        for a in (d.get("attachments") or [])
    ]
    return Message(
        platform_id    = str(d.get("platform_id", "")),
        sender         = d.get("sender", ""),
        recipient      = d.get("recipient", ""),
        body           = d.get("body", ""),
        timestamp      = _dt(d.get("timestamp")) or datetime.now(timezone.utc),
        is_sent        = bool(d.get("is_sent", False)),
        attachments    = attachments,
        service        = d.get("service", "sms"),
        read           = bool(d.get("read", True)),
        sms_type       = int(d.get("sms_type", 0) or 0),
        thread_id      = int(d.get("thread_id", 0) or 0),
        status         = int(d.get("status", -1) or -1),
        subject        = d.get("subject", ""),
        from_addresses = d.get("from_addresses") or [],
        to_addresses   = d.get("to_addresses") or [],
        cc_addresses   = d.get("cc_addresses") or [],
    )


def _call(d: dict) -> CallRecord:
    return CallRecord(
        number           = d.get("number", ""),
        call_type        = d.get("call_type", "incoming"),
        timestamp        = _dt(d.get("timestamp")) or datetime.now(timezone.utc),
        duration_seconds = int(d.get("duration_seconds") or d.get("duration") or 0),
        name             = d.get("name"),
    )


def _calendar_event(d: dict) -> CalendarEvent:
    return CalendarEvent(
        uid            = d.get("uid"),
        title          = d.get("title", ""),
        start          = _dt(d.get("start")) or datetime.now(timezone.utc),
        end            = _dt(d.get("end")) or datetime.now(timezone.utc),
        notes          = d.get("notes") or d.get("description"),
        location       = d.get("location"),
        all_day        = bool(d.get("all_day", False)),
        recurrence_rule= d.get("recurrence_rule") or d.get("rrule"),
    )


def _note(d: dict) -> Note:
    return Note(
        title    = d.get("title", ""),
        body     = d.get("body", ""),
        created  = _dt(d.get("created") or d.get("created_at")),
        modified = _dt(d.get("modified") or d.get("modified_at")),
        folder   = d.get("folder"),
    )


def _alarm(d: dict) -> Alarm:
    return Alarm(
        hour        = int(d.get("hour", 0)),
        minute      = int(d.get("minute", 0)),
        label       = d.get("label", ""),
        enabled     = bool(d.get("enabled", True)),
        repeat_days = d.get("repeat_days") or d.get("days") or [],
        sound       = d.get("sound") or d.get("ringtone"),
    )


def _reminder(d: dict) -> Reminder:
    return Reminder(
        title     = d.get("title", ""),
        notes     = d.get("notes"),
        due       = _dt(d.get("due")),
        completed = bool(d.get("completed", False)),
        priority  = int(d.get("priority", 0)),
        list_name = d.get("list_name"),
        uid       = d.get("uid"),
    )


def _bookmark(d: dict) -> Bookmark:
    return Bookmark(
        title  = d.get("title", ""),
        url    = d.get("url", ""),
        folder = d.get("folder"),
        added  = _dt(d.get("added")),
    )


def _blocked(d: dict) -> BlockedNumber:
    return BlockedNumber(number=d.get("number", ""))


def _contact_group(d: dict) -> ContactGroup:
    return ContactGroup(
        title=d.get("title", ""),
        group_id=int(d["group_id"]) if d.get("group_id") is not None else None,
        account_name=d.get("account_name"),
        account_type=d.get("account_type"),
        visible=bool(d.get("visible", True)),
        notes=d.get("notes"),
        member_count=int(d.get("member_count", 0)),
    )


def _clipboard_item(d: dict) -> ClipboardItem:
    return ClipboardItem(
        text=d.get("text", ""),
        mime_type=d.get("mime_type"),
    )


def _browser_history(d: dict) -> BrowserHistoryEntry:
    visited = _dt(d.get("last_visited"))
    return BrowserHistoryEntry(
        url=d.get("url", ""),
        title=d.get("title", ""),
        visited=visited if visited else datetime(1970, 1, 1, tzinfo=timezone.utc),
        visit_count=int(d.get("visit_count", 1)),
        browser="chrome",
    )


def _installed_app(d: dict) -> InstalledApp:
    return InstalledApp(
        package_name=d.get("package_name", ""),
        app_name=d.get("app_name", ""),
        version_name=d.get("version_name"),
        version_code=int(d["version_code"]) if d.get("version_code") is not None else None,
        apk_size=int(d.get("apk_size", 0)),
        install_time=int(d["install_time"]) if d.get("install_time") is not None else None,
        update_time=int(d["update_time"]) if d.get("update_time") is not None else None,
        is_system=bool(d.get("is_system", False)),
    )


# Registry
_FACTORY = {
    "contacts":        _contact,
    "contact_groups":  _contact_group,
    "sms":             _message,
    "whatsapp":        _message,
    "telegram":        _message,
    "calls":           _call,
    "calendar":        _calendar_event,
    "notes":           _note,
    "alarms":          _alarm,
    "reminders":       _reminder,
    "bookmarks":       _bookmark,
    "blocked":         _blocked,
    "browser_history": _browser_history,
    "clipboard":       _clipboard_item,
    "installed_apps":  _installed_app,
}
