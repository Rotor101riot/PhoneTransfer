"""
convert_ringtones.py

Ringtone format conversion for cross-platform transfers.

iOS ringtone rules:
  - Container : MPEG-4 Audio (.m4r)
  - Codec     : AAC
  - Max length: 30 seconds (enforced by iOS — longer files are silently ignored)

Android ringtone rules:
  - Any standard audio format works (MP3 is most universal)
  - No length restriction
"""

from pathlib import Path

from core.ffmpeg_wrapper import run, FFmpegError  # noqa: F401

IOS_MAX_SECONDS = 30


def to_m4r(input_path: str, output_path: str, trim_to_30s: bool = True) -> str:
    """
    Convert any audio file to an iOS-compatible M4R ringtone.
    Output extension is forced to .m4r regardless of what is passed.

    Args:
        input_path:  Source audio file.
        output_path: Desired output path (extension overridden to .m4r).
        trim_to_30s: Enforce the iOS 30-second maximum (default True).

    Returns:
        Absolute path to the .m4r file.
    """
    input_p  = Path(input_path)
    output_p = Path(output_path).with_suffix(".m4r")

    if not input_p.exists():
        raise FileNotFoundError(f"Input ringtone file not found: {input_path}")

    output_p.parent.mkdir(parents=True, exist_ok=True)

    args = ["-y", "-i", str(input_p)]
    if trim_to_30s:
        args += ["-t", str(IOS_MAX_SECONDS)]
    args += ["-c:a", "aac", "-b:a", "128k", str(output_p)]

    run(args)
    return str(output_p.resolve())


def to_mp3(input_path: str, output_path: str) -> str:
    """
    Convert any audio file to MP3 for Android ringtone use.
    MP3 is the safest universal choice across all Android OEMs.
    """
    input_p  = Path(input_path)
    output_p = Path(output_path).with_suffix(".mp3")

    if not input_p.exists():
        raise FileNotFoundError(f"Input ringtone file not found: {input_path}")

    output_p.parent.mkdir(parents=True, exist_ok=True)

    args = [
        "-y", "-i", str(input_p),
        "-c:a", "libmp3lame", "-b:a", "192k",
        str(output_p),
    ]
    run(args)
    return str(output_p.resolve())


def to_m4a(input_path: str, output_path: str) -> str:
    """
    Convert an iOS M4R ringtone to M4A for Android.
    M4A is an acceptable alternative to MP3 on modern Android.
    """
    input_p  = Path(input_path)
    output_p = Path(output_path).with_suffix(".m4a")

    if not input_p.exists():
        raise FileNotFoundError(f"Input ringtone file not found: {input_path}")

    output_p.parent.mkdir(parents=True, exist_ok=True)

    args = [
        "-y", "-i", str(input_p),
        "-c:a", "aac", "-b:a", "192k",
        str(output_p),
    ]
    run(args)
    return str(output_p.resolve())
