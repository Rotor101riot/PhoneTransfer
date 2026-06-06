"""
convert_sms.py

Converts Message objects between SMS/MMS/iMessage formats.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from core.normalization_schema import Message, MessageAttachment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SERVICE_MAP: dict[str, Literal["sms", "mms", "imessage"]] = {
    "sms":       "sms",
    "mms":       "mms",
    "imessage":  "imessage",
    "imsg":      "imessage",
    "i-message": "imessage",
    "apple":     "imessage",
    "text":      "sms",
    "multimedia": "mms",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def messages_to_smil(messages: list[Message]) -> str:  # noqa: ARG001
    """
    Placeholder — SMIL generation is handled by the MMS injector layer.
    Returns an empty string.
    """
    return ""


def normalize_service(service: str) -> Literal["sms", "mms", "imessage"]:
    """
    Map any variant string to the canonical Literal value.

    Case-insensitive.  Unknown values default to ``"sms"``.
    """
    return _SERVICE_MAP.get(service.lower().strip(), "sms")


def message_to_dict(msg: Message) -> dict:
    """
    Serialize a Message to a plain dict suitable for JSON persistence.

    - ``timestamp`` → ISO 8601 string
    - ``attachments`` → list of dicts
    - ``local_path`` → str or None
    """
    attachments = []
    for att in msg.attachments:
        attachments.append({
            "filename":  att.filename,
            "mime_type": att.mime_type,
            "data":      att.data.hex() if att.data is not None else None,
            "local_path": str(att.local_path) if att.local_path is not None else None,
        })
    return {
        "platform_id": msg.platform_id,
        "sender":      msg.sender,
        "recipient":   msg.recipient,
        "body":        msg.body,
        "timestamp":   msg.timestamp.isoformat(),
        "is_sent":     msg.is_sent,
        "attachments": attachments,
        "service":     msg.service,
        "read":        msg.read,
    }


def dict_to_message(d: dict) -> Message:
    """
    Deserialize a Message from a plain dict (inverse of :func:`message_to_dict`).

    Parses ISO 8601 timestamps and reconstructs MessageAttachment objects.
    """
    attachments: list[MessageAttachment] = []
    for a in d.get("attachments", []):
        data_hex = a.get("data")
        lp = a.get("local_path")
        attachments.append(MessageAttachment(
            filename=a["filename"],
            mime_type=a["mime_type"],
            data=bytes.fromhex(data_hex) if data_hex is not None else None,
            local_path=Path(lp) if lp is not None else None,
        ))

    return Message(
        platform_id=d["platform_id"],
        sender=d["sender"],
        recipient=d["recipient"],
        body=d["body"],
        timestamp=datetime.fromisoformat(d["timestamp"]),
        is_sent=d["is_sent"],
        attachments=attachments,
        service=normalize_service(d.get("service", "sms")),
        read=d.get("read", True),
    )


def messages_to_json(messages: list[Message], path: Path) -> Path:
    """
    Serialize a list of Message objects to a JSON file at *path*.

    Returns *path*.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [message_to_dict(m) for m in messages]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def messages_from_json(path: Path) -> list[Message]:
    """
    Read and parse a JSON file written by :func:`messages_to_json`.

    Returns a list of Message objects.
    """
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [dict_to_message(d) for d in raw]


def filter_by_service(messages: list[Message], service: str) -> list[Message]:
    """
    Return only messages whose ``service`` field equals *service* exactly.
    """
    return [m for m in messages if m.service == service]


def imessage_to_sms(msg: Message) -> Message:
    """
    Return a copy of *msg* with ``service="sms"``.

    Attachments that have neither ``data`` nor ``local_path`` are dropped,
    since they cannot be delivered over SMS.  The message body is unchanged.
    """
    valid_attachments = [
        att for att in msg.attachments
        if att.data is not None or att.local_path is not None
    ]
    return Message(
        platform_id=msg.platform_id,
        sender=msg.sender,
        recipient=msg.recipient,
        body=msg.body,
        timestamp=msg.timestamp,
        is_sent=msg.is_sent,
        attachments=valid_attachments,
        service="sms",
        read=msg.read,
    )
