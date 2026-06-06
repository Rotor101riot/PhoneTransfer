"""
convert_video.py

Video format normalisation via FFmpeg.
Primary operation: stream-copy (remux) MOV → MP4 with no re-encode.
Falls back to full transcode when codecs are incompatible with MP4.
"""

from pathlib import Path

from core.ffmpeg_wrapper import run, FFmpegError  # noqa: F401

SUPPORTED_INPUT = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".3gp", ".webm"}


def remux_to_mp4(input_path: str, output_path: str) -> str:
    """
    Remux video to MP4 without re-encoding. Fast and lossless.
    Suitable for H.264/H.265 MOV files from iPhone — no quality loss.

    Raises:
        FileNotFoundError  — input missing
        FFmpegError        — remux failed (use transcode_to_mp4 as fallback)
    """
    input_p  = Path(input_path)
    output_p = Path(output_path).with_suffix(".mp4")

    if not input_p.exists():
        raise FileNotFoundError(f"Input video file not found: {input_path}")

    output_p.parent.mkdir(parents=True, exist_ok=True)

    args = [
        "-y", "-i", str(input_p),
        "-c", "copy",            # stream copy — no re-encode
        "-movflags", "+faststart",  # MP4 index at front for streaming
        str(output_p),
    ]
    run(args)
    return str(output_p.resolve())


def transcode_to_mp4(
    input_path: str,
    output_path: str,
    video_codec: str = "libx264",
    audio_codec: str = "aac",
    crf: int = 23,
) -> str:
    """
    Full re-encode to MP4. Use when remux_to_mp4 fails due to codec
    incompatibility (e.g. ProRes, HEVC in an AVI container).

    Args:
        crf: Constant Rate Factor for libx264 (18=high quality, 28=small file).
    """
    input_p  = Path(input_path)
    output_p = Path(output_path).with_suffix(".mp4")

    if not input_p.exists():
        raise FileNotFoundError(f"Input video file not found: {input_path}")

    output_p.parent.mkdir(parents=True, exist_ok=True)

    args = [
        "-y", "-i", str(input_p),
        "-c:v", video_codec, "-crf", str(crf),
        "-c:a", audio_codec,
        "-movflags", "+faststart",
        str(output_p),
    ]
    run(args)
    return str(output_p.resolve())


def convert(input_path: str, output_path: str) -> str:
    """
    Convenience wrapper: tries remux first, falls back to transcode.
    Caller-facing function for the pipeline.
    """
    try:
        return remux_to_mp4(input_path, output_path)
    except FFmpegError:
        return transcode_to_mp4(input_path, output_path)
