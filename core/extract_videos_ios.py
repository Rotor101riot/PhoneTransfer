"""
extract_videos_ios.py
Extract video files from an iOS device using AFC (Apple File Conduit).

Strategy:
  1. Connect via AFC (works on stock devices for the Media folder).
  2. Recursively list /var/mobile/Media/DCIM/ and filter by video extension.
  3. Pull each matching file to the staging directory.
  4. Build a MediaFile for every pulled file.

If the device is jailbroken (is_privileged=True) AFC2 gives full filesystem
access, but the DCIM path is the same so no special branch is needed here.
"""

import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".mpeg", ".mpg", ".wmv"}
)
# AFC root is /var/mobile/Media, so DCIM appears as /DCIM inside the AFC share
DCIM_ROOT = "/DCIM"


def _list_videos_recursive(afc, remote_dir: str) -> list[str]:
    """Return a flat list of AFC-relative paths that have a video extension."""
    results: list[str] = []
    entries = afc.list_dir(remote_dir)
    if not entries:
        return results

    for entry in entries:
        if entry in (".", ".."):
            continue
        remote_path = f"{remote_dir}/{entry}"
        info = afc.stat(remote_path)
        if info is None:
            continue

        if info.get("st_ifmt") == "S_IFDIR":
            results.extend(_list_videos_recursive(afc, remote_path))
        else:
            suffix = PurePosixPath(entry).suffix.lower()
            if suffix in VIDEO_EXTENSIONS:
                results.append(remote_path)

    return results


def extract(device_id: str, staging_dir: Path, is_privileged: bool) -> list[MediaFile]:
    """
    Extract videos from an iOS device.

    Parameters
    ----------
    device_id:
        The device UDID / serial as reported by libimobiledevice.
    staging_dir:
        Local directory where pulled files are written.
    is_privileged:
        True if the device is jailbroken (AFC2 available).

    Returns
    -------
    list[MediaFile]
        One entry per successfully pulled video file.
    """
    staging_dir = Path(staging_dir)
    videos_dir = staging_dir / "videos_ios"
    videos_dir.mkdir(parents=True, exist_ok=True)

    results: list[MediaFile] = []

    try:
        from core.device_connection_cache import get_broker
        from core.afc_connector import AFCConnector
        broker = get_broker(device_id)
        afc = AFCConnector(broker)
    except Exception as exc:
        logger.error("extract_videos_ios: cannot open AFC for %s: %s", device_id, exc)
        return results

    logger.info("extract_videos_ios: scanning %s for video files …", DCIM_ROOT)
    remote_paths = _list_videos_recursive(afc, DCIM_ROOT)
    logger.info("extract_videos_ios: found %d video file(s)", len(remote_paths))

    for remote_path in remote_paths:
        filename = PurePosixPath(remote_path).name
        local_path = videos_dir / filename

        # Avoid collisions from different subdirectories.
        if local_path.exists():
            stem = PurePosixPath(remote_path).stem
            suffix = PurePosixPath(remote_path).suffix
            counter = 1
            while local_path.exists():
                local_path = videos_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        if not afc.pull_file(remote_path, local_path):
            logger.warning("extract_videos_ios: failed to pull %s", remote_path)
            continue

        # Attempt to get mtime from AFC stat.
        created: datetime | None = None
        try:
            info = afc.stat(remote_path)
            if info:
                mtime = info.get("st_mtime")
                if mtime is not None:
                    created = datetime.fromtimestamp(int(mtime) / 1e9, tz=timezone.utc)
        except Exception:
            pass

        mime_type, _ = mimetypes.guess_type(filename)
        if mime_type is None:
            mime_type = "video/mp4"

        media_file = MediaFile(
            filename=filename,
            mime_type=mime_type,
            local_path=local_path,
            created=created,
            album="videos",
            latitude=None,
            longitude=None,
        )
        results.append(media_file)
        logger.debug("extract_videos_ios: pulled %s", remote_path)

    logger.info("extract_videos_ios: extracted %d video(s) from device %s", len(results), device_id)
    return results
