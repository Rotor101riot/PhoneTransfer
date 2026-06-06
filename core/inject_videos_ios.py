"""
inject_videos_ios.py
Inject video files onto an iOS device using AFC (Apple File Conduit).

Strategy:
  1. Filter the supplied items to those whose mime_type starts with "video/".
  2. Connect via IOSServiceBroker / AFCConnector (no bare pymobiledevice3 imports
     at module level so the module always loads regardless of pymobiledevice3 state).
  3. Ensure the target directory exists on the device.
  4. Push each local file to /var/mobile/Media/DCIM/PhoneTransfer/<filename>.
  5. Trigger a photo-library rescan so iOS indexes the new files.

Note: A full photo-library rescan on stock (non-jailbroken) devices is not
possible without a special entitlement; the user is instructed to restart the
device or use a third-party app to refresh the Photos library.
"""

import logging
from pathlib import Path

from core.afc_connector import AFCConnector
from core.ios_service_broker import IOSServiceBroker
from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)

TARGET_DIR = "/var/mobile/Media/DCIM/PhoneTransfer"


def _trigger_rescan(broker: IOSServiceBroker, is_privileged: bool) -> None:
    """
    Attempt to trigger a photo-library rescan.

    On jailbroken devices we try to restart the relevant daemon via the
    diagnostics relay.  On stock devices we log a notice only.
    """
    if not is_privileged:
        logger.info(
            "inject_videos_ios: photo library rescan requires a jailbroken device. "
            "Please restart your iPhone or use a third-party app to refresh the Photos library."
        )
        return

    try:
        from pymobiledevice3.services.diagnostics import DiagnosticsService  # type: ignore[import]
        lockdown = broker.get_lockdown()
        diag = DiagnosticsService(lockdown)
        diag.restart()
        logger.info("inject_videos_ios: device restart triggered to rescan photo library")
    except Exception as exc:
        logger.warning(
            "inject_videos_ios: could not trigger photo-library rescan: %s. "
            "A manual device restart may be required.",
            exc,
        )


def inject(device_id: str, items: list[MediaFile], staging_dir: Path, is_privileged: bool) -> int:
    """
    Inject video files onto an iOS device.

    Parameters
    ----------
    device_id:
        The device UDID / serial as reported by libimobiledevice.
    items:
        Normalised MediaFile objects to inject (all types accepted; non-video
        items are silently skipped).
    staging_dir:
        Local staging directory (unused here, kept for API consistency).
    is_privileged:
        True if the device is jailbroken (diagnostics relay available).

    Returns
    -------
    int
        Number of video files successfully pushed to the device.
    """
    video_items = [item for item in items if item.mime_type.startswith("video/")]
    if not video_items:
        logger.info("inject_videos_ios: no video items to inject")
        return 0

    logger.info(
        "inject_videos_ios: preparing %d video(s) for device %s",
        len(video_items),
        device_id,
    )

    broker = IOSServiceBroker(udid=device_id)
    try:
        try:
            afc = AFCConnector(broker)
        except Exception as exc:
            logger.error("inject_videos_ios: cannot open AFC service: %s", exc)
            return 0

        afc.makedirs(TARGET_DIR)

        injected = 0
        for item in video_items:
            local_path = item.local_path
            if local_path is None or not local_path.exists():
                logger.warning(
                    "inject_videos_ios: local file not found, skipping: %s",
                    local_path,
                )
                continue

            remote_path = f"{TARGET_DIR}/{item.filename}"
            if afc.push_file(local_path, remote_path):
                injected += 1
                logger.debug(
                    "inject_videos_ios: pushed %s -> %s", local_path, remote_path
                )
            else:
                logger.warning(
                    "inject_videos_ios: push failed for %s", item.filename
                )

        logger.info(
            "inject_videos_ios: injected %d/%d video(s) to device %s",
            injected,
            len(video_items),
            device_id,
        )

        if injected > 0:
            _trigger_rescan(broker, is_privileged)

        return injected
    finally:
        broker.close()
