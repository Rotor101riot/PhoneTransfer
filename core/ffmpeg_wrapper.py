"""
ffmpeg_wrapper.py

Single point of entry for all FFmpeg subprocess calls.
Resolution order for the binary:
  1. /bin/ffmpeg.exe  (bundled)
  2. System PATH

All converter modules import run() from here — they never call subprocess
directly. This ensures consistent error handling, timeout enforcement,
and binary path resolution across the entire project.
"""

import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
_BUNDLED = BASE_DIR / "bin" / "ffmpeg.exe"


# ── Exceptions ────────────────────────────────────────────────────────────────

class FFmpegError(Exception):
    """Raised when FFmpeg exits with a non-zero return code."""


class FFmpegNotFoundError(FFmpegError):
    """Raised when the FFmpeg binary cannot be located."""


# ── Binary resolution ─────────────────────────────────────────────────────────

def find_ffmpeg() -> str:
    """
    Return the path to the FFmpeg binary.
    Checks the bundled /bin directory first, then falls back to system PATH.
    Raises FFmpegNotFoundError if neither is available.
    """
    if _BUNDLED.exists():
        return str(_BUNDLED)

    system_path = shutil.which("ffmpeg")
    if system_path:
        return system_path

    raise FFmpegNotFoundError(
        "FFmpeg not found. Place ffmpeg.exe in the /bin directory "
        "or install FFmpeg and ensure it is on your system PATH."
    )


# ── Execution ─────────────────────────────────────────────────────────────────

def run(
    args: List[str],
    timeout: int = 300,
    extra_env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    """
    Execute FFmpeg with the given argument list.
    `args` must NOT include the binary name itself — just the flags.

    Example:
        run(["-y", "-i", "input.ogg", "-b:a", "192k", "output.mp3"])

    Raises:
        FFmpegNotFoundError  — binary missing
        FFmpegError          — non-zero exit code
        FFmpegError          — timeout exceeded
    """
    binary = find_ffmpeg()
    cmd = [binary] + args

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=extra_env,
        )
    except subprocess.TimeoutExpired:
        raise FFmpegError(f"FFmpeg timed out after {timeout}s. Command: {' '.join(cmd)}")
    except FileNotFoundError:
        raise FFmpegNotFoundError(f"FFmpeg binary not executable at path: {binary}")

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise FFmpegError(
            f"FFmpeg exited {result.returncode}:\n{stderr}"
        )

    return result


def version() -> str:
    """
    Return the first line of `ffmpeg -version`.
    Useful for startup checks and logging.
    """
    result = run(["-version"])
    first_line = result.stdout.decode("utf-8", errors="replace").splitlines()[0]
    return first_line


def probe_duration(file_path: str) -> Optional[float]:
    """
    Return the duration of a media file in seconds using ffprobe,
    or None if ffprobe is not available or the file has no duration.
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        # Try alongside the ffmpeg binary
        ffmpeg_path = Path(find_ffmpeg())
        candidate = ffmpeg_path.parent / "ffprobe.exe"
        if candidate.exists():
            ffprobe = str(candidate)

    if not ffprobe:
        return None

    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        return float(result.stdout.decode().strip())
    except (ValueError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
