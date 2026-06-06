"""
convert_imessage_to_mms.py

Converts iMessage-format Message objects to MMS-compatible format for Android injection.
iMessages with attachments become MMS; plain text iMessages become SMS.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.normalization_schema import Message

# ---------------------------------------------------------------------------
# E.164-like normalization
# ---------------------------------------------------------------------------

_DIGITS_ONLY = re.compile(r"^\d+$")


def _to_e164_like(number: str) -> str:
    """
    Normalize a phone number to E.164-like format.

    If the string contains only digits and has 10 or more characters a leading
    ``+`` is prepended.  Numbers that already start with ``+`` are returned
    unchanged (after stripping whitespace).  Anything that doesn't look like a
    plain digit string (e.g. "self", email addresses) is returned as-is.
    """
    stripped = number.strip()
    if stripped.startswith("+"):
        return stripped
    if _DIGITS_ONLY.match(stripped) and len(stripped) >= 10:
        return "+" + stripped
    return stripped


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------

def imessage_to_mms(msg: Message) -> Message:
    """
    Convert a single iMessage-format Message to SMS or MMS.

    Rules:
    - If the message has attachments **or** the body exceeds 160 characters,
      ``service`` is set to ``"mms"``.
    - Otherwise ``service`` is set to ``"sms"``.
    - ``sender`` and ``recipient`` are normalized to E.164-like format.
    - The original Message is not mutated; a new Message is returned.
    """
    has_attachments = bool(msg.attachments)
    use_mms = has_attachments or len(msg.body.encode("utf-8")) > 160

    return Message(
        platform_id=msg.platform_id,
        sender=_to_e164_like(msg.sender),
        recipient=_to_e164_like(msg.recipient),
        body=msg.body,
        timestamp=msg.timestamp,
        is_sent=msg.is_sent,
        attachments=list(msg.attachments),
        service="mms" if use_mms else "sms",
        read=msg.read,
    )


def convert_batch(messages: list[Message]) -> list[Message]:
    """
    Apply :func:`imessage_to_mms` to all messages whose ``service`` is
    ``"imessage"``.  Non-iMessage messages are passed through unchanged.

    Returns the full list (preserving order).
    """
    result: list[Message] = []
    for msg in messages:
        if msg.service == "imessage":
            result.append(imessage_to_mms(msg))
        else:
            result.append(msg)
    return result


# ---------------------------------------------------------------------------
# Attachment helpers
# ---------------------------------------------------------------------------

def extract_attachment_paths(msg: Message) -> list[Path]:
    """
    Return a list of all non-None ``local_path`` values from the message's
    attachments.
    """
    return [att.local_path for att in msg.attachments if att.local_path is not None]


# ---------------------------------------------------------------------------
# Thread grouping
# ---------------------------------------------------------------------------

def group_by_thread(messages: list[Message]) -> dict[str, list[Message]]:
    """
    Group messages by conversation thread.

    Thread key = ``str(tuple(sorted({sender, recipient})))``.
    Messages within each thread are sorted by ``timestamp`` (ascending).

    Returns a dict mapping thread key → sorted message list.
    """
    threads: dict[str, list[Message]] = {}
    for msg in messages:
        key = str(tuple(sorted({msg.sender, msg.recipient})))
        threads.setdefault(key, []).append(msg)
    for thread_msgs in threads.values():
        thread_msgs.sort(key=lambda m: m.timestamp)
    return threads
