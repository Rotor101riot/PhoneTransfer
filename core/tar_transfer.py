"""
tar_transfer.py

Fast bulk file transfer from an Android device using ``tar`` + ``adb exec-out``.

Instead of calling ``adb pull`` for every file individually — one ADB
round-trip per file, which is very slow for large photo libraries — this
module streams a gzipped tar archive of the requested directories directly
from the device and pipes it directly into Python's ``tarfile`` module for
on-the-fly extraction.  No temporary archive file is created — neither on
the device nor on the PC — so peak disk usage is just the size of the
extracted files themselves.

Usage
-----
    from core.tar_transfer import probe_tar, pull_dirs_with_tar

    if probe_tar(serial, adb):
        local_files, ok = pull_dirs_with_tar(
            serial,
            ["/sdcard/DCIM", "/sdcard/Pictures", "/sdcard/Movies"],
            staging_sub,
            adb,
            timeout=300,
        )
        if ok:
            ...  # local_files is a list of Path objects

Why ``adb exec-out`` instead of ``adb shell``?
-----------------------------------------------
``adb shell`` converts line endings (LF → CR LF) on Windows, which corrupts
binary data.  ``adb exec-out`` streams raw bytes with no translation, making
it suitable for piping binary formats like tar.
"""

from __future__ import annotations

import logging
import subprocess
import tarfile
from pathlib import Path

from core.adb_manager import ADBManager

logger = logging.getLogger(__name__)

# Name of the staging sub-directory created inside the caller's local_dir
# where tar contents are extracted.
_EXTRACT_SUBDIR = "_tar_extract"


def probe_tar(serial: str, adb: ADBManager) -> bool:
    """
    Return True if the device has ``tar`` available on ``$PATH``.

    Uses ``which tar`` — fast and reliable on busybox-based Android shells.
    Falls back gracefully: returns False on any error.
    """
    try:
        stdout, _, rc = adb.shell(serial, "which tar", timeout=10)
        return rc == 0 and "tar" in stdout.strip()
    except Exception:
        return False


def pull_dirs_with_tar(
    serial: str,
    remote_dirs: list[str],
    local_dir: Path,
    adb: ADBManager,
    timeout: int = 300,
) -> tuple[list[Path], bool]:
    """
    Pull *remote_dirs* from the device to *local_dir* via a single tar stream.

    Uses ``adb exec-out tar -czf - -C / <relative_dirs>`` to pipe a gzipped
    tar archive directly into a local file, then extracts it.

    Parameters
    ----------
    serial:
        ADB device serial.
    remote_dirs:
        Absolute device paths to archive, e.g. ``["/sdcard/DCIM"]``.
    local_dir:
        Local directory under which an ``_tar_extract/`` sub-directory is
        created and the archive contents are expanded.
    adb:
        ADBManager instance (used for its configured ADB executable path).
    timeout:
        Seconds to allow for the streaming subprocess.

    Returns
    -------
    (list[Path], True)
        List of all extracted file paths on success.
    ([], False)
        On any error — the caller should fall back to individual ``adb pull``.
    """
    local_dir.mkdir(parents=True, exist_ok=True)
    extract_dir = local_dir / _EXTRACT_SUBDIR

    # Convert absolute device paths to paths relative to "/" so that
    # ``tar -C /`` can accept them.  "/sdcard/DCIM" → "sdcard/DCIM".
    rel_dirs = [d.lstrip("/") for d in remote_dirs]

    adb_exe = str(adb._adb)
    cmd = [
        adb_exe, "-s", serial, "exec-out",
        "tar", "-czf", "-", "-C", "/",
        *rel_dirs,
    ]

    logger.info(
        "[tar_transfer] Streaming tar from device dirs: %s",
        ", ".join(remote_dirs),
    )

    try:
        # Stream the tar archive directly into tarfile without writing the
        # compressed archive to disk first.  tarfile's streaming mode ("r|gz")
        # reads from a file-like object (the subprocess stdout pipe) and
        # decompresses/extracts on the fly, so only the final files ever touch
        # the filesystem — halving peak disk usage versus the write-then-extract
        # approach.
        extract_dir.mkdir(parents=True, exist_ok=True)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|gz") as tf:
                tf.extractall(extract_dir)
        finally:
            # Always drain stderr and wait for the process to exit so we get a
            # valid returncode and don't leave zombie processes.
            _, stderr_bytes = proc.communicate(timeout=timeout)

        if proc.returncode != 0:
            err = stderr_bytes.decode("utf-8", errors="replace").strip()
            logger.warning(
                "[tar_transfer] tar command exited %d: %s",
                proc.returncode, err,
            )
            return [], False

        # Collect all extracted regular files.
        extracted: list[Path] = [
            p for p in extract_dir.rglob("*") if p.is_file()
        ]
        logger.info(
            "[tar_transfer] Extracted %d file(s) from archive", len(extracted)
        )
        return extracted, True

    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        logger.warning(
            "[tar_transfer] Timed out after %ds — falling back to adb pull",
            timeout,
        )
        return [], False

    except tarfile.TarError as exc:
        logger.warning("[tar_transfer] Archive extraction error: %s", exc)
        proc.kill()
        proc.communicate()
        return [], False

    except Exception as exc:
        logger.warning("[tar_transfer] Unexpected error: %s", exc)
        try:
            proc.kill()
            proc.communicate()
        except Exception:
            pass
        return [], False


