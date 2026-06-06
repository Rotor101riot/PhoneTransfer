"""
convert_whatsapp.py

WhatsApp backup conversion stubs.

WhatsApp uses end-to-end encryption and crypt15 key files stored in the app's
private storage.  Reading requires root on Android or an authorized iTunes
backup on iOS.  This module provides helpers for working with exported WhatsApp
chat text files (.txt) which can be exported by WhatsApp directly via
Chat → Export Chat.

crypt15 decryption: the decryption key lives at
    /data/data/com.whatsapp/files/key   (Android, root required)
or can be extracted from an iTunes backup on iOS.  This tool does NOT perform
crypt15 decryption; it only parses the plaintext .txt export format.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.normalization_schema import Message, MessageAttachment

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class WhatsAppMessage:
    """A single message parsed from a WhatsApp exported chat .txt file."""

    timestamp: datetime
    sender: str
    body: str
    is_media: bool = False
    media_filename: Optional[str] = None


# ---------------------------------------------------------------------------
# Export-line parsing
# ---------------------------------------------------------------------------

# Format 1 (US/international): "MM/DD/YYYY, HH:MM AM/PM - Sender: message"
_PATTERN_US = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{2,4}),\s+"       # date
    r"(\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M)"   # time (with optional seconds)
    r"\s+-\s+"                                # separator
    r"([^:]+):\s*"                            # sender
    r"(.*)$",                                 # body
    re.IGNORECASE,
)

# Format 2 (ISO/European): "[DD/MM/YYYY, HH:MM:SS] Sender: message"
_PATTERN_ISO = re.compile(
    r"^\[(\d{1,2}/\d{1,2}/\d{2,4}),\s+"     # date inside bracket
    r"(\d{1,2}:\d{2}(?::\d{2})?)\]"          # time inside bracket
    r"\s+"                                    # space
    r"([^:]+):\s*"                            # sender
    r"(.*)$",                                 # body
)

# Marker WhatsApp inserts for omitted media
_MEDIA_OMITTED = re.compile(r"<Media omitted>|<.+ omitted>", re.IGNORECASE)
# Attached file: "filename (file attached)"
_ATTACHED_FILE = re.compile(r"^(.+?)\s+\(file attached\)$", re.IGNORECASE)


def _parse_dt_us(date_str: str, time_str: str) -> datetime:
    """Parse US-format date + 12-hour time into a UTC-aware datetime."""
    time_str = time_str.strip()
    # Try with seconds first, then without
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%y %I:%M:%S %p",
                "%m/%d/%Y %I:%M %p",   "%m/%d/%y %I:%M %p"):
        try:
            raw = f"{date_str} {time_str}"
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"Cannot parse US datetime: {date_str!r} {time_str!r}")


def _parse_dt_iso(date_str: str, time_str: str) -> datetime:
    """Parse European/ISO-format date + 24-hour time into a UTC-aware datetime."""
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%y %H:%M:%S",
                "%d/%m/%Y %H:%M",    "%d/%m/%y %H:%M"):
        try:
            return datetime.strptime(f"{date_str} {time_str}", fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"Cannot parse ISO datetime: {date_str!r} {time_str!r}")


def parse_export_line(line: str) -> WhatsAppMessage | None:
    """
    Parse a single line from a WhatsApp exported chat .txt file.

    Supported formats:
    - ``"MM/DD/YYYY, HH:MM AM/PM - Sender: message"``
    - ``"[DD/MM/YYYY, HH:MM:SS] Sender: message"``

    Returns ``None`` if the line does not match either pattern (e.g. it is a
    continuation of the previous message).
    """
    line = line.rstrip("\r\n")

    # Try US format
    m = _PATTERN_US.match(line)
    if m:
        date_str, time_str, sender, body = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            ts = _parse_dt_us(date_str, time_str.strip())
        except ValueError:
            return None
        return _build_wamsg(ts, sender.strip(), body.strip())

    # Try ISO/European format
    m = _PATTERN_ISO.match(line)
    if m:
        date_str, time_str, sender, body = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            ts = _parse_dt_iso(date_str, time_str.strip())
        except ValueError:
            return None
        return _build_wamsg(ts, sender.strip(), body.strip())

    return None


def _build_wamsg(ts: datetime, sender: str, body: str) -> WhatsAppMessage:
    """Construct a WhatsAppMessage, detecting media markers."""
    if _MEDIA_OMITTED.search(body):
        return WhatsAppMessage(timestamp=ts, sender=sender, body=body, is_media=True)
    attached = _ATTACHED_FILE.match(body)
    if attached:
        return WhatsAppMessage(
            timestamp=ts,
            sender=sender,
            body=body,
            is_media=True,
            media_filename=attached.group(1),
        )
    return WhatsAppMessage(timestamp=ts, sender=sender, body=body)


# ---------------------------------------------------------------------------
# Full-file parser
# ---------------------------------------------------------------------------

def parse_export_file(path: Path) -> list[WhatsAppMessage]:
    """
    Read a WhatsApp exported chat .txt file and return a list of
    WhatsAppMessage objects.

    Multi-line messages (continuation lines that don't start with a timestamp)
    are appended to the previous message's body separated by ``\\n``.
    """
    path = Path(path)
    messages: list[WhatsAppMessage] = []
    current: WhatsAppMessage | None = None

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parsed = parse_export_line(raw_line)
        if parsed is not None:
            if current is not None:
                messages.append(current)
            current = parsed
        else:
            # Continuation line
            if current is not None:
                current.body = current.body + "\n" + raw_line.rstrip("\r\n")

    if current is not None:
        messages.append(current)

    return messages


# ---------------------------------------------------------------------------
# Conversion to normalized Message
# ---------------------------------------------------------------------------

def whatsapp_msg_to_message(wm: WhatsAppMessage) -> Message:
    """
    Convert a WhatsAppMessage to a normalized :class:`~core.normalization_schema.Message`.

    - ``service`` is always ``"sms"`` (WhatsApp SMS export has no MMS metadata).
    - ``is_sent`` is ``True`` when ``sender`` is ``"You"`` or ``"you"``.
    - ``recipient`` is set to ``""`` (not available in single-chat exports).
    - Media messages include a stub MessageAttachment if ``media_filename`` is set.
    """
    is_sent = wm.sender.lower() == "you"
    attachments: list[MessageAttachment] = []
    if wm.is_media and wm.media_filename:
        attachments.append(MessageAttachment(
            filename=wm.media_filename,
            mime_type="application/octet-stream",
            data=None,
            local_path=None,
        ))
    return Message(
        platform_id=str(int(wm.timestamp.timestamp() * 1000)),
        sender=wm.sender,
        recipient="",
        body=wm.body,
        timestamp=wm.timestamp,
        is_sent=is_sent,
        attachments=attachments,
        service="sms",
        read=True,
    )


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------

def _wamsg_to_dict(wm: WhatsAppMessage) -> dict:
    return {
        "timestamp":      wm.timestamp.isoformat(),
        "sender":         wm.sender,
        "body":           wm.body,
        "is_media":       wm.is_media,
        "media_filename": wm.media_filename,
    }


def _dict_to_wamsg(d: dict) -> WhatsAppMessage:
    return WhatsAppMessage(
        timestamp=datetime.fromisoformat(d["timestamp"]),
        sender=d["sender"],
        body=d.get("body", ""),
        is_media=bool(d.get("is_media", False)),
        media_filename=d.get("media_filename"),
    )


def messages_to_json(messages: list[WhatsAppMessage], path: Path) -> Path:
    """Write WhatsApp messages to a JSON file at *path*. Returns *path*."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([_wamsg_to_dict(m) for m in messages], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def messages_from_json(path: Path) -> list[WhatsAppMessage]:
    """Read WhatsApp messages from a JSON file written by :func:`messages_to_json`."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [_dict_to_wamsg(d) for d in raw]
