"""
inject_photos_android.py

Injects MediaFile records into an Android device connected via USB/ADB.

Strategy
--------
1.  All files are staged into a temporary local directory with conflict-
    resolved names (HEIC files are converted to JPEG on the fly).

2.  The entire directory is pushed in a single ``adb push`` command.  This
    is dramatically faster than per-file pushes because ADB's sync protocol
    pipelines the transfer, eliminating per-file command/response overhead.

3.  After the push, the Android Media Scanner is triggered so the gallery
    app discovers the new files immediately.

HEIC conversion is parallelised across multiple threads to maximise CPU
utilisation while file I/O is handled by ADB.

Return value: count of files successfully pushed to the device.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

from core.adb_manager import ADBManager
from core.config_loader import get_config
from core.normalization_schema import MediaFile
from convert.convert_heic import convert as convert_heic, HEIC_EXTENSIONS

logger = logging.getLogger(__name__)

_DEVICE_DCIM_DIR  = "/sdcard/DCIM/PhoneTransfer"
_DEVICE_STAGE_DIR = "/sdcard/PhoneTransfer"
_MAX_HEIC_WORKERS = 4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_remote_name(
    existing_names: set[str],
    filename: str,
) -> str:
    """
    Return a remote filename that is not already in *existing_names*.

    Conflict resolution: insert a counter before the extension, e.g.
    ``photo.jpg`` → ``photo_1.jpg`` → ``photo_2.jpg`` …
    """
    if filename not in existing_names:
        return filename

    stem = PurePosixPath(filename).stem
    suffix = PurePosixPath(filename).suffix
    counter = 1
    while True:
        candidate = f"{stem}_{counter}{suffix}"
        if candidate not in existing_names:
            return candidate
        counter += 1


def _stage_file(
    item: MediaFile,
    stage_dir: Path,
    resolved_name: str,
    heic_tmp_dir: Path,
) -> bool:
    """
    Stage a single file into *stage_dir* under *resolved_name*.

    For HEIC files, converts to JPEG first (writing into *heic_tmp_dir*),
    then links/copies the converted file.  Returns True on success.
    """
    try:
        local_path = item.local_path
        if not local_path.exists():
            logger.warning(
                "inject_photos_android: local file not found, skipping: %s",
                local_path,
            )
            return False

        # Convert HEIC/HEIF → JPEG so older Android devices can display them
        if local_path.suffix.lower() in HEIC_EXTENSIONS:
            try:
                jpg_name = local_path.stem + ".jpg"
                jpg_path = heic_tmp_dir / jpg_name
                convert_heic(str(local_path), str(jpg_path))
                local_path = jpg_path
                logger.debug(
                    "inject_photos_android: converted HEIC→JPEG: %s",
                    item.filename,
                )
            except Exception as exc:
                logger.warning(
                    "inject_photos_android: HEIC conversion failed for %s, "
                    "pushing original: %s", item.filename, exc,
                )

        dest = stage_dir / resolved_name

        # Try hard link first (instant, no copy), fall back to copy
        try:
            os.link(local_path, dest)
        except OSError:
            shutil.copy2(local_path, dest)

        return True

    except Exception as exc:
        logger.warning(
            "inject_photos_android: staging error for %s: %s",
            item.filename, exc,
        )
        return False


def _trigger_media_scanner(
    adb: ADBManager,
    serial: str,
    remote_names: list[str],
) -> None:
    """
    Notify the Android Media Scanner about newly pushed files.

    Two strategies are used:
    a) Per-file broadcast — works on Android 4–9.
    b) Volume scan via content provider — Android 10+ (MediaStore).
    """
    # a) Per-file MEDIA_SCANNER_SCAN_FILE broadcasts
    for name in remote_names:
        remote_path = f"{_DEVICE_DCIM_DIR}/{name}"
        _, _, rc = adb.shell(
            serial,
            f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
            f"-d 'file://{remote_path}'",
            timeout=15,
        )
        if rc != 0:
            logger.debug(
                "inject_photos_android: per-file media scan broadcast "
                "returned rc=%d for %s", rc, remote_path,
            )

    # b) Full volume rescan (Android 10+ MediaStore equivalent)
    _, _, rc = adb.shell(
        serial,
        "content call --method scan_volume --uri content://media --arg external",
        timeout=30,
    )
    if rc != 0:
        logger.debug(
            "inject_photos_android: content scan_volume returned rc=%d "
            "(non-fatal on older Android)", rc,
        )

    logger.info(
        "inject_photos_android: media scanner triggered for %d file(s).",
        len(remote_names),
    )


# ---------------------------------------------------------------------------
# Public inject function
# ---------------------------------------------------------------------------

def inject(
    serial: str,
    items: list[MediaFile],
    staging_dir: Path,
    is_rooted: bool = False,
) -> int:
    """
    Push media files to the Android device identified by *serial*.

    Parameters
    ----------
    serial:      ADB device serial.
    items:       MediaFile records to push.  Items whose ``local_path`` does
                 not exist on disk are skipped.
    staging_dir: Local directory for temporary files (not used for photos,
                 kept for API consistency).
    is_rooted:   Currently unused for photos — reserved for future use.

    Returns
    -------
    int: Number of files successfully pushed.
    """
    if not items:
        logger.info("inject_photos_android: no media files to inject — done.")
        return 0

    logger.info(
        "inject_photos_android: preparing %d file(s) for device %s",
        len(items), serial,
    )

    try:
        cfg = get_config()
        adb = ADBManager(cfg)
    except Exception as exc:
        logger.error("inject_photos_android: failed to initialise ADB: %s", exc)
        return 0

    # ── 1. Ensure target directory exists on device ───────────────────────────
    for device_dir in (_DEVICE_DCIM_DIR, _DEVICE_STAGE_DIR):
        try:
            _, stderr, rc = adb.shell(serial, f"mkdir -p {device_dir}")
            if rc != 0:
                logger.warning(
                    "inject_photos_android: mkdir -p %s returned rc=%d: %s",
                    device_dir, rc, stderr.strip(),
                )
        except Exception as exc:
            logger.warning(
                "inject_photos_android: mkdir -p %s error: %s", device_dir, exc
            )

    # ── 2. Stage all files into a temp directory ──────────────────────────────
    # Using a temp dir lets us push everything in one `adb push` command,
    # which pipelines the USB transfer and avoids per-file subprocess overhead.
    batch_dir = Path(tempfile.mkdtemp(prefix="pt_photo_batch_"))
    heic_tmp_dir = Path(tempfile.mkdtemp(prefix="pt_heic_"))

    used_names: set[str] = set()
    staged: list[tuple[str, MediaFile]] = []  # (resolved_name, item)

    # Resolve names first (must be sequential for conflict resolution)
    name_map: list[tuple[MediaFile, str]] = []
    for item in items:
        filename = item.filename
        # Pre-resolve HEIC extension to .jpg for name uniqueness
        if item.local_path.suffix.lower() in HEIC_EXTENSIONS:
            filename = item.local_path.stem + ".jpg"
        resolved = _resolve_remote_name(used_names, filename)
        used_names.add(resolved)
        name_map.append((item, resolved))

    # Stage files in parallel (HEIC conversion is CPU-bound and benefits)
    def _do_stage(pair: tuple[MediaFile, str]) -> tuple[str, bool]:
        item, resolved = pair
        ok = _stage_file(item, batch_dir, resolved, heic_tmp_dir)
        return resolved, ok

    with ThreadPoolExecutor(
        max_workers=_MAX_HEIC_WORKERS,
        thread_name_prefix="heic-conv",
    ) as pool:
        for resolved_name, ok in pool.map(_do_stage, name_map):
            if ok:
                staged.append((resolved_name, None))  # type: ignore[arg-type]

    staged_count = len(staged)
    logger.info(
        "inject_photos_android: staged %d/%d files into batch dir",
        staged_count, len(items),
    )

    # ── 3. Single batch push ──────────────────────────────────────────────────
    pushed_count = 0
    pushed_names: list[str] = []

    if staged_count > 0:
        # adb push <local_dir>/. <remote_dir>/ pushes all contents
        # Use a generous timeout: 5 min base + 30s per file for large batches
        timeout = max(300, staged_count * 30)
        ok = adb.push(serial, batch_dir, _DEVICE_DCIM_DIR + "/", timeout=timeout)
        if ok:
            pushed_count = staged_count
            pushed_names = [name for name, _ in staged]
            logger.info(
                "inject_photos_android: batch push succeeded — %d files",
                pushed_count,
            )
        else:
            logger.error(
                "inject_photos_android: batch push failed — falling back to per-file push"
            )
            # Fallback: push files individually
            for name, _ in staged:
                local_file = batch_dir / name
                remote_path = f"{_DEVICE_DCIM_DIR}/{name}"
                if adb.push(serial, local_file, remote_path, timeout=300):
                    pushed_count += 1
                    pushed_names.append(name)
                else:
                    logger.warning(
                        "inject_photos_android: per-file push failed for %s", name
                    )

    logger.info(
        "inject_photos_android: pushed %d/%d file(s) to %s",
        pushed_count, len(items), _DEVICE_DCIM_DIR,
    )

    # ── 4. Clean up temp directories ─────────────────────────────────────────
    for tmp in (batch_dir, heic_tmp_dir):
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass

    # ── 5. Trigger media scanner ──────────────────────────────────────────────
    if pushed_names:
        try:
            _trigger_media_scanner(adb, serial, pushed_names)
        except Exception as exc:
            logger.warning(
                "inject_photos_android: media scanner trigger failed: %s", exc
            )

    return pushed_count
