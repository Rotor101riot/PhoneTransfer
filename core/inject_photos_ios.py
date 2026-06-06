"""
inject_photos_ios.py

Injects MediaFile records (photos, videos) into an iOS device connected via USB.

Strategy
--------
Two-phase injection for maximum compatibility:

Phase 1 — File push via AFC
  All files are pushed into a dedicated DCIM subfolder:
      /var/mobile/Media/DCIM/999PTRNS/

  The folder name follows the DCF 8-character convention (NNN + 5 alphanum)
  so that iOS Photos recognises it as a valid camera-roll subdirectory.

Phase 2 — Photos.sqlite direct injection
  After the files are on-device, asset records are inserted directly into
  /var/mobile/Media/PhotoData/Photos.sqlite so that photos appear in the
  Photos app immediately without relying on medialibraryd re-scanning DCIM.

  The database is pulled via AFC, modified locally, and pushed back.  A
  wal_checkpoint(TRUNCATE) is run before the push so the device receives a
  single self-contained SQLite file with no pending WAL.

After both phases, the injector posts the syncStatusChanged notification to
prompt the Photos app to reload its data from the updated database.

The jailbroken flag is accepted for interface consistency but is not required
for this importer — standard AFC is sufficient for both DCIM and PhotoData.

Return value: count of individual media files successfully pushed to the device.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.afc_connector import AFCConnector
from core.ios_service_broker import IOSServiceBroker
from core.normalization_schema import MediaFile
from core.photos_sqlite_injector import inject_into_photos_db
from core.pmd3_asyncio import pmd3_run

logger = logging.getLogger(__name__)

# Remote paths
# Folder name follows the DCF 8-char convention (NNN + 5 alphanum) so that
# iOS Photos recognises it as a valid camera roll subdirectory.
_DCIM_DIR = "/var/mobile/Media/DCIM"
_TARGET_FOLDER = "/var/mobile/Media/DCIM/999PTRNS"

# Notification posted to ask Photos to re-scan DCIM
_PHOTOS_SCAN_NOTIFICATION = "com.apple.itunes-client.syncStatusChanged"


# ---------------------------------------------------------------------------
# Public inject function
# ---------------------------------------------------------------------------

def inject(
    udid: str,
    items: list[MediaFile],
    staging_dir: Path,
    is_jailbroken: bool = False,
) -> int:
    """
    Push media files to the iOS device's DCIM directory.

    Parameters
    ----------
    udid:           iOS device UDID.
    items:          Media files to push.  Each must have a valid local_path.
    staging_dir:    Local directory for temporary files used by the
                    Photos.sqlite pull/modify/push cycle.
    is_jailbroken:  Accepted but not required; standard AFC covers DCIM.

    Returns
    -------
    int: Number of files successfully pushed to the device.
    """
    if not items:
        logger.info("inject_photos_ios: no media files to inject — done.")
        return 0

    logger.info(
        "inject_photos_ios: preparing %d file(s) for device %s.",
        len(items),
        udid,
    )

    broker = IOSServiceBroker(udid=udid)
    try:
        return _do_inject(broker, items, staging_dir)
    finally:
        broker.close()


# ---------------------------------------------------------------------------
# Internal implementation
# ---------------------------------------------------------------------------

def _do_inject(
    broker: IOSServiceBroker,
    items: list[MediaFile],
    staging_dir: Path,
) -> int:
    """Push files to DCIM, inject DB records, notify Photos; return success count."""
    try:
        afc = AFCConnector(broker)
    except Exception as exc:
        logger.error("inject_photos_ios: failed to open AFC service: %s", exc)
        return 0

    # ── Detect iOS version for schema guard context ─────────────────────────
    ios_version: str | None = broker.get_ios_version()
    if ios_version:
        logger.info("inject_photos_ios: target device iOS %s", ios_version)

    # ── Phase 1: Ensure target directory exists ─────────────────────────────
    _ensure_dcim_target(afc)

    # ── Phase 1: Build collision-avoidance set ──────────────────────────────
    existing: set[str] = set(afc.list_dir(_TARGET_FOLDER))

    # ── Phase 1: Push each file ─────────────────────────────────────────────
    pushed_items: list[MediaFile] = []
    pushed_names: list[str] = []
    for item in items:
        try:
            filename, ok = _push_one_tracked(afc, item, existing)
            if ok:
                pushed_items.append(item)
                pushed_names.append(filename)
        except Exception as exc:
            logger.warning(
                "inject_photos_ios: unexpected error pushing %s: %s",
                item.filename,
                exc,
            )

    pushed = len(pushed_items)
    logger.info(
        "inject_photos_ios: pushed %d / %d file(s) to %s.",
        pushed,
        len(items),
        _TARGET_FOLDER,
    )

    if pushed == 0:
        return 0

    # ── Phase 1b: Verify pushed file sizes on device ───────────────────────
    _verify_pushed_files(afc, pushed_items, pushed_names)

    # ── Phase 2: Inject asset records into Photos.sqlite ───────────────────
    dcim_subfolder = _TARGET_FOLDER.split("/")[-1]  # "999PTRNS"
    try:
        db_inserted = inject_into_photos_db(
            broker=broker,
            items=pushed_items,
            dcim_subfolder=dcim_subfolder,
            pushed_filenames=pushed_names,
            staging_dir=staging_dir,
            ios_version=ios_version,
        )
        logger.info(
            "inject_photos_ios: %d record(s) written to Photos.sqlite.", db_inserted
        )
        if db_inserted > 0:
            logger.info(
                "inject_photos_ios: photos will appear in the Photos app. "
                "If they show as grey with a cloud icon, a device reboot will "
                "clear the iCloud sync state and generate thumbnails."
            )
    except Exception as exc:
        logger.warning(
            "inject_photos_ios: Photos.sqlite injection failed (files still on "
            "device, will appear after next Photos scan): %s",
            exc,
        )

    # ── Phase 3: Notify Photos to reload ───────────────────────────────────
    _notify_photos(broker)

    return pushed


def _ensure_dcim_target(afc: AFCConnector) -> None:
    """Create /var/mobile/Media/DCIM/999PTRNS if it does not exist."""
    # Check whether the top-level DCIM exists to log something useful
    dcim_entries = afc.list_dir(_DCIM_DIR)
    if not dcim_entries and not afc.exists(_DCIM_DIR):
        logger.warning(
            "inject_photos_ios: DCIM directory not found at %s — "
            "will attempt to create it anyway.",
            _DCIM_DIR,
        )

    ok = afc.makedirs(_TARGET_FOLDER)
    if ok:
        logger.debug(
            "inject_photos_ios: target directory ready: %s", _TARGET_FOLDER
        )
    else:
        logger.warning(
            "inject_photos_ios: could not create target directory %s — "
            "pushes may fail.",
            _TARGET_FOLDER,
        )


def _push_one_tracked(
    afc: AFCConnector,
    item: MediaFile,
    existing: set[str],
) -> tuple[str, bool]:
    """
    Push a single MediaFile to the device.

    Returns (pushed_filename, success).
    Mutates *existing* to track the filename once pushed so subsequent files
    with the same name are de-duplicated correctly within this session.
    """
    if not item.local_path.exists():
        logger.warning(
            "inject_photos_ios: local file not found, skipping: %s",
            item.local_path,
        )
        return item.filename, False

    filename = _unique_filename(item.filename, existing)
    device_path = f"{_TARGET_FOLDER}/{filename}"

    logger.debug(
        "inject_photos_ios: pushing %s -> %s", item.local_path, device_path
    )
    ok = afc.push_file(item.local_path, device_path)
    if ok:
        existing.add(filename)
        return filename, True
    else:
        logger.warning(
            "inject_photos_ios: push failed for %s.", item.local_path
        )
        return filename, False


def _unique_filename(original: str, existing: set[str]) -> str:
    """
    Return a filename that does not collide with any name in *existing*.

    Appends _1, _2, … before the extension until a free name is found.
    """
    if original not in existing:
        return original

    stem = Path(original).stem
    suffix = Path(original).suffix
    counter = 1
    while True:
        candidate = f"{stem}_{counter}{suffix}"
        if candidate not in existing:
            return candidate
        counter += 1


def _verify_pushed_files(
    afc: AFCConnector,
    pushed_items: list[MediaFile],
    pushed_names: list[str],
) -> None:
    """
    Spot-check that every pushed file exists on the device with the correct
    size.  Logs a warning for each mismatch; does not abort the transfer.
    """
    verified = 0
    mismatches: list[str] = []

    for item, name in zip(pushed_items, pushed_names):
        device_path = f"{_TARGET_FOLDER}/{name}"
        local_size = item.local_path.stat().st_size if item.local_path.exists() else -1
        info = afc.stat(device_path)
        if info is None:
            mismatches.append(f"{name}: not found on device after push")
            continue
        dev_size = int(info.get("st_size", 0) or 0)
        if dev_size != local_size:
            mismatches.append(
                f"{name}: size mismatch (local={local_size}B, device={dev_size}B)"
            )
        else:
            verified += 1

    if mismatches:
        for msg in mismatches:
            logger.warning("inject_photos_ios: verification — %s", msg)
        logger.warning(
            "inject_photos_ios: %d/%d file(s) passed size verification — "
            "%d mismatch(es) logged above.",
            verified, len(pushed_items), len(mismatches),
        )
    else:
        logger.info(
            "inject_photos_ios: all %d file(s) verified on device (sizes match).",
            verified,
        )


def _notify_photos(broker: IOSServiceBroker) -> None:
    """
    Post com.apple.itunes-client.syncStatusChanged to ask the Photos app to
    re-scan the DCIM directory.  Entirely best-effort — errors are suppressed.
    """
    try:
        np = broker.get_notification_proxy()
        if hasattr(np, "notify_post"):
            pmd3_run(np.notify_post(_PHOTOS_SCAN_NOTIFICATION))
        elif hasattr(np, "post"):
            pmd3_run(np.post(_PHOTOS_SCAN_NOTIFICATION))
        else:
            logger.debug(
                "inject_photos_ios: notification proxy has no known post method."
            )
            return
        logger.debug(
            "inject_photos_ios: posted %s to trigger Photos re-scan.",
            _PHOTOS_SCAN_NOTIFICATION,
        )
    except Exception as exc:
        logger.debug(
            "inject_photos_ios: could not post Photos scan notification "
            "(non-fatal): %s",
            exc,
        )
