"""
inject_videos_android.py
Inject video files onto an Android device via ADB.

Strategy:
  1. Filter supplied items to those whose mime_type starts with "video/".
  2. Stage all files into a temporary local directory with conflict-resolved
     names, then push the entire directory in a single ``adb push`` command.
     This pipelines the USB transfer and avoids per-file subprocess overhead.
  3. Trigger a MediaScanner broadcast so videos appear in the gallery
     immediately.

Requires:
  - ADB available at the path returned by core.config_loader.get_config().
  - USB debugging enabled on the target device.
"""

import logging
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from core.adb_manager import ADBManager
from core.config_loader import get_config
from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)

TARGET_DIR = "/sdcard/DCIM/PhoneTransfer"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_remote_name(
    existing_names: set[str],
    filename: str,
) -> str:
    """Return a unique remote filename, appending _N on conflict."""
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


def _trigger_media_scan(adb: ADBManager, serial: str, remote_names: list[str]) -> None:
    """Notify the Android MediaScanner about newly pushed files."""
    for name in remote_names:
        remote_path = f"{TARGET_DIR}/{name}"
        _, _, rc = adb.shell(
            serial,
            f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
            f"-d 'file://{remote_path}'",
            timeout=15,
        )
        if rc != 0:
            logger.debug(
                "inject_videos_android: media scan broadcast failed for %s (rc=%d)",
                remote_path, rc,
            )

    # Volume rescan for Android 10+
    _, _, rc = adb.shell(
        serial,
        "content call --method scan_volume --uri content://media --arg external",
        timeout=30,
    )
    if rc != 0:
        logger.debug(
            "inject_videos_android: content scan_volume returned rc=%d "
            "(non-fatal on older Android)", rc,
        )
    logger.info(
        "inject_videos_android: media scanner triggered for %d file(s).",
        len(remote_names),
    )


# ---------------------------------------------------------------------------
# Public inject function
# ---------------------------------------------------------------------------

def inject(device_id: str, items: list[MediaFile], staging_dir: Path, is_privileged: bool) -> int:
    """
    Inject video files onto an Android device.

    Parameters
    ----------
    device_id:
        The ADB serial of the target device.
    items:
        Normalised MediaFile objects (all types accepted; non-video items are
        silently skipped).
    staging_dir:
        Local staging directory (kept for API consistency; not used directly).
    is_privileged:
        True if the device is rooted (reserved for future use).

    Returns
    -------
    int
        Number of files successfully pushed to the device.
    """
    video_items = [item for item in items if item.mime_type.startswith("video/")]
    if not video_items:
        logger.info("inject_videos_android: no video items to inject")
        return 0

    config = get_config()
    adb = ADBManager(config)

    # Ensure remote dir
    _, stderr, rc = adb.shell(device_id, f"mkdir -p {TARGET_DIR}")
    if rc != 0:
        logger.error(
            "inject_videos_android: failed to create %s: %s",
            TARGET_DIR, stderr.strip(),
        )
        return 0

    # ── Stage files into a batch directory ────────────────────────────────────
    batch_dir = Path(tempfile.mkdtemp(prefix="pt_video_batch_"))
    used_names: set[str] = set()
    staged_names: list[str] = []

    for item in video_items:
        local_path = Path(item.local_path)
        if not local_path.exists():
            logger.warning("inject_videos_android: local file not found: %s", local_path)
            continue

        resolved = _resolve_remote_name(used_names, item.filename)
        used_names.add(resolved)
        dest = batch_dir / resolved

        # Hard link preferred (zero-copy), fallback to copy for cross-device
        try:
            os.link(local_path, dest)
        except OSError:
            shutil.copy2(local_path, dest)

        staged_names.append(resolved)

    if not staged_names:
        shutil.rmtree(batch_dir, ignore_errors=True)
        return 0

    logger.info(
        "inject_videos_android: staged %d video(s) into batch dir",
        len(staged_names),
    )

    # ── Single batch push ─────────────────────────────────────────────────────
    # Generous timeout: videos can be large
    timeout = max(600, len(staged_names) * 60)
    injected = 0
    pushed_names: list[str] = []

    ok = adb.push(device_id, batch_dir, TARGET_DIR + "/", timeout=timeout)
    if ok:
        injected = len(staged_names)
        pushed_names = staged_names
        logger.info(
            "inject_videos_android: batch push succeeded — %d files", injected
        )
    else:
        logger.error(
            "inject_videos_android: batch push failed — falling back to per-file"
        )
        for name in staged_names:
            local_file = batch_dir / name
            remote_path = f"{TARGET_DIR}/{name}"
            if adb.push(device_id, local_file, remote_path, timeout=600):
                injected += 1
                pushed_names.append(name)
            else:
                logger.warning(
                    "inject_videos_android: per-file push failed for %s", name
                )

    # ── Cleanup + media scanner ───────────────────────────────────────────────
    shutil.rmtree(batch_dir, ignore_errors=True)

    if pushed_names:
        try:
            _trigger_media_scan(adb, device_id, pushed_names)
        except Exception as exc:
            logger.warning(
                "inject_videos_android: media scanner trigger failed: %s", exc
            )

    logger.info(
        "inject_videos_android: injected %d/%d video(s) to device %s",
        injected, len(video_items), device_id,
    )
    return injected
