"""
convert_audio.py

General-purpose audio format conversion via FFmpeg.
Used by convert_ringtones and anywhere else audio transcoding is needed.
"""

import logging
import tempfile
from pathlib import Path
from typing import Optional

from core.ffmpeg_wrapper import run, FFmpegError  # noqa: F401 (re-exported for callers)

logger = logging.getLogger(__name__)

SUPPORTED_INPUT  = {".ogg", ".m4a", ".mp3", ".aac", ".caf", ".wav", ".flac", ".opus", ".m4r", ".amr"}
SUPPORTED_OUTPUT = {".m4a", ".mp3", ".aac", ".wav", ".ogg", ".amr"}

# AMR magic byte sequences
_AMR_NB_MAGIC: bytes = b"#!AMR\n"
_AMR_WB_MAGIC: bytes = b"#!AMR-WB\n"


def convert(
    input_path: str,
    output_path: str,
    bitrate: str = "192k",
    sample_rate: Optional[int] = None,
) -> str:
    """
    Transcode audio from any supported input format to any supported output format.

    Args:
        input_path:   Source audio file (must exist).
        output_path:  Destination file (created or overwritten).
        bitrate:      Target audio bitrate (default "192k").
        sample_rate:  Optional output sample rate in Hz (e.g. 44100).

    Returns:
        Absolute path to the output file.

    Raises:
        FileNotFoundError  — input file missing
        ValueError         — unsupported input or output format
        FFmpegError        — conversion failed
    """
    input_p  = Path(input_path)
    output_p = Path(output_path)

    if not input_p.exists():
        raise FileNotFoundError(f"Input audio file not found: {input_path}")
    if input_p.suffix.lower() not in SUPPORTED_INPUT:
        raise ValueError(
            f"Unsupported input audio format: '{input_p.suffix}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_INPUT))}"
        )
    if output_p.suffix.lower() not in SUPPORTED_OUTPUT:
        raise ValueError(
            f"Unsupported output audio format: '{output_p.suffix}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_OUTPUT))}"
        )

    output_p.parent.mkdir(parents=True, exist_ok=True)

    args = ["-y", "-i", str(input_p), "-b:a", bitrate]
    if sample_rate:
        args += ["-ar", str(sample_rate)]
    args.append(str(output_p))

    run(args)
    return str(output_p.resolve())


def normalize_voicemail_audio(audio_bytes: bytes, work_dir: Path) -> bytes:
    """
    Normalize voicemail audio to 8 kHz AMR-NB for cross-platform compatibility.

    iOS records voicemails as AMR-NB, but some carriers / newer iOS versions
    can produce AMR-WB (wideband, 16 kHz).  Android voicemail injectors and
    most MVNO visual-voicemail apps only accept AMR-NB.  This function detects
    the variant by magic bytes and transcodes AMR-WB → AMR-NB via FFmpeg.

    Parameters
    ----------
    audio_bytes:
        Raw bytes of the .amr file as read from the iOS backup.
    work_dir:
        Scratch directory for intermediate FFmpeg files.

    Returns
    -------
    AMR-NB bytes ready for injection.  Returns *audio_bytes* unchanged if
    it is already AMR-NB, empty, or not a recognised AMR format.
    """
    if not audio_bytes:
        return audio_bytes

    if audio_bytes.startswith(_AMR_NB_MAGIC):
        return audio_bytes  # already AMR-NB — nothing to do

    if not audio_bytes.startswith(_AMR_WB_MAGIC):
        logger.debug("normalize_voicemail_audio: unrecognised magic — passthrough")
        return audio_bytes

    # AMR-WB detected — transcode to 8 kHz AMR-NB
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            dir=work_dir, suffix=".amr", delete=False
        ) as src_f:
            src_f.write(audio_bytes)
            src_path = Path(src_f.name)

        out_path = src_path.with_suffix(".nb.amr")
        try:
            run([
                "-y", "-i", str(src_path),
                "-ar", "8000",
                "-ac", "1",
                "-b:a", "12.2k",
                str(out_path),
            ])
            result = out_path.read_bytes()
            logger.debug(
                "normalize_voicemail_audio: AMR-WB→NB transcode: %d→%d bytes",
                len(audio_bytes), len(result),
            )
            return result
        except FFmpegError as exc:
            logger.warning(
                "normalize_voicemail_audio: FFmpeg transcode failed (%s) "
                "— returning original AMR-WB bytes",
                exc,
            )
            return audio_bytes
        finally:
            for p in (src_path, out_path):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
    except Exception as exc:
        logger.warning(
            "normalize_voicemail_audio: unexpected error (%s) — passthrough", exc
        )
        return audio_bytes
