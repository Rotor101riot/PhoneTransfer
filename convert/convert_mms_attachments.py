"""
convert_mms_attachments.py

Handles preparation of MMS attachment files for Android injection.
Converts image/audio/video attachments to Android-compatible formats.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from core.normalization_schema import Message, MessageAttachment

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extension maps
# ---------------------------------------------------------------------------

AUDIO_TRANSCODE_EXTS: dict[str, str] = {
    ".caf": ".mp3",
    ".m4a": ".mp3",
    ".aac": ".mp3",
}

IMAGE_CONVERT_EXTS: dict[str, str] = {
    ".heic": ".jpg",
    ".heif": ".jpg",
}

VIDEO_TRANSCODE_EXTS: dict[str, str] = {
    ".mov": ".mp4",
    ".m4v": ".mp4",
}


# ---------------------------------------------------------------------------
# Core attachment preparation
# ---------------------------------------------------------------------------

def prepare_attachment(attachment: MessageAttachment, staging_dir: Path) -> MessageAttachment:
    """
    Prepare a single MessageAttachment for Android MMS injection.

    Conversion rules (applied when ``attachment.local_path`` exists):

    - **HEIC/HEIF images** → JPEG via :func:`convert.convert_heic.convert`
    - **CAF/M4A/AAC audio** → MP3 via :func:`convert.convert_audio.convert`
    - **MOV/M4V video** → MP4 via :func:`convert.convert_video.convert`
    - **All other files** → copied as-is into *staging_dir*

    If conversion fails a warning is logged and the original attachment is
    returned unchanged.  If no ``local_path`` is set the attachment is
    returned as-is.

    Returns a new MessageAttachment (the original is not mutated).
    """
    staging_dir = Path(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    src: Path | None = attachment.local_path
    if src is None or not src.exists():
        return attachment

    suffix = src.suffix.lower()

    try:
        if suffix in IMAGE_CONVERT_EXTS:
            from convert.convert_heic import convert as heic_convert
            out_suffix = IMAGE_CONVERT_EXTS[suffix]
            dst = staging_dir / (src.stem + out_suffix)
            heic_convert(str(src), str(dst))
            return MessageAttachment(
                filename=dst.name,
                mime_type="image/jpeg",
                data=attachment.data,
                local_path=dst,
            )

        if suffix in AUDIO_TRANSCODE_EXTS:
            from convert.convert_audio import convert as audio_convert
            out_suffix = AUDIO_TRANSCODE_EXTS[suffix]
            dst = staging_dir / (src.stem + out_suffix)
            audio_convert(str(src), str(dst))
            return MessageAttachment(
                filename=dst.name,
                mime_type="audio/mpeg",
                data=attachment.data,
                local_path=dst,
            )

        if suffix in VIDEO_TRANSCODE_EXTS:
            from convert.convert_video import convert as video_convert
            out_suffix = VIDEO_TRANSCODE_EXTS[suffix]
            dst = staging_dir / (src.stem + out_suffix)
            video_convert(str(src), str(dst))
            return MessageAttachment(
                filename=dst.name,
                mime_type="video/mp4",
                data=attachment.data,
                local_path=dst,
            )

        # No conversion needed — copy as-is
        dst = staging_dir / src.name
        if dst.resolve() != src.resolve():
            shutil.copy2(str(src), str(dst))
        return MessageAttachment(
            filename=dst.name,
            mime_type=attachment.mime_type,
            data=attachment.data,
            local_path=dst,
        )

    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Attachment conversion failed for %s (%s): %s — using original.",
            src.name,
            suffix,
            exc,
        )
        return attachment


# ---------------------------------------------------------------------------
# Message-level helpers
# ---------------------------------------------------------------------------

def prepare_message_attachments(msg: Message, staging_dir: Path) -> Message:
    """
    Apply :func:`prepare_attachment` to every attachment in *msg*.

    Returns a new Message with the updated attachment list.
    The original Message is not mutated.
    """
    prepared = [prepare_attachment(att, staging_dir) for att in msg.attachments]
    return Message(
        platform_id=msg.platform_id,
        sender=msg.sender,
        recipient=msg.recipient,
        body=msg.body,
        timestamp=msg.timestamp,
        is_sent=msg.is_sent,
        attachments=prepared,
        service=msg.service,
        read=msg.read,
    )


def prepare_batch(messages: list[Message], staging_dir: Path) -> list[Message]:
    """
    Apply :func:`prepare_message_attachments` to every message that has at
    least one attachment.  Messages without attachments are passed through
    unchanged.

    Returns the full list (preserving order).
    """
    result: list[Message] = []
    for msg in messages:
        if msg.attachments:
            result.append(prepare_message_attachments(msg, staging_dir))
        else:
            result.append(msg)
    return result
