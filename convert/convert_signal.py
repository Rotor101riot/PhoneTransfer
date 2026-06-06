"""
convert_signal.py

Signal message backup conversion stubs.

Signal uses end-to-end encryption with keys stored in the Secure Enclave (iOS)
or Android Keystore (Android).  Direct migration is not possible without the
Signal Transfer protocol (Signal → Signal in-app QR transfer).  This module
provides serialization for any plaintext metadata that may be recoverable from
unencrypted backup fields.

WARNING: Full Signal migration is not supported due to Secure Enclave /
Keystore key inaccessibility.  Message content remains encrypted and cannot be
read or transferred by this tool.  Use Signal's built-in device-transfer
feature (Settings → Account → Transfer or restore account) for a supported
migration path.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_WARNED = False


def _warn_once() -> None:
    global _WARNED
    if not _WARNED:
        log.warning(
            "Signal migration is not supported: message keys are stored in the iOS "
            "Secure Enclave or Android Keystore and cannot be accessed without "
            "device-specific hardware. Use Signal's built-in Transfer feature instead."
        )
        _WARNED = True


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SignalMessage:
    """
    Plaintext metadata recoverable from an unencrypted Signal backup field.

    Actual message bodies remain encrypted and are not stored here.
    """

    thread_id: str
    sender: str
    body: str          # plaintext body if available; empty string otherwise
    timestamp: datetime
    is_sent: bool
    has_attachments: bool = False


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def signal_msg_to_dict(m: SignalMessage) -> dict:
    """Serialize a SignalMessage to a plain dict (``timestamp`` → ISO string)."""
    _warn_once()
    return {
        "thread_id":       m.thread_id,
        "sender":          m.sender,
        "body":            m.body,
        "timestamp":       m.timestamp.isoformat(),
        "is_sent":         m.is_sent,
        "has_attachments": m.has_attachments,
    }


def dict_to_signal_msg(d: dict) -> SignalMessage:
    """Deserialize a SignalMessage from a plain dict."""
    return SignalMessage(
        thread_id=d["thread_id"],
        sender=d["sender"],
        body=d.get("body", ""),
        timestamp=datetime.fromisoformat(d["timestamp"]),
        is_sent=bool(d.get("is_sent", False)),
        has_attachments=bool(d.get("has_attachments", False)),
    )


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def messages_to_json(messages: list[SignalMessage], path: Path) -> Path:
    """
    Write a list of SignalMessage objects to a JSON file at *path*.

    Returns *path*.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([signal_msg_to_dict(m) for m in messages], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def messages_from_json(path: Path) -> list[SignalMessage]:
    """
    Read and parse a JSON file written by :func:`messages_to_json`.

    Returns a list of SignalMessage objects.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [dict_to_signal_msg(d) for d in raw]
