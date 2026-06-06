"""
extract_videos_android.py
Extract video files from an Android device via ADB and the MediaStore content
provider.

Strategy:
  1. Query content://media/external/video/media for all video entries,
     capturing the on-device file path, display name, date_taken /
     date_added, and bucket (album) name.  ``date_taken`` is preferred
     because Android's MediaScanner populates it from the MP4/MOV
     moov/mvhd creation_time box — i.e. the actual recording timestamp.
     ``date_added`` (when the file was first indexed) is used only as a
     fallback; it can be years later than the true recording time for
     videos that were copied in from another device.
  2. Pull each file with "adb pull" into the local staging directory.
  3. Build a MediaFile for every successfully pulled file.

Requires:
  - ADB available at the path returned by core.config_loader.get_config().
  - USB debugging enabled on the target device.
"""

import logging
import mimetypes
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from core.config_loader import get_config
from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)

MEDIA_URI = "content://media/external/video/media"
PROJECTION = "_id,_display_name,_data,date_taken,date_added,bucket_display_name"


def _adb(device_id: str, *args: str, adb_path: str = "adb") -> subprocess.CompletedProcess:
    cmd = [adb_path, "-s", device_id, *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def _parse_content_rows(output: str) -> list[dict[str, str]]:
    """
    Parse the tabular output of 'adb shell content query'.

    Each result row looks like:
        Row: 0 _id=12, _display_name=VID_001.mp4, _data=/sdcard/DCIM/..., ...
    """
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("Row:"):
            continue
        # Drop the "Row: N " prefix.
        try:
            payload = line.split(" ", 2)[2]
        except IndexError:
            continue

        record: dict[str, str] = {}
        # Fields are comma-separated but values may not contain commas in
        # practice.  We split on ", " followed by a known key pattern to be
        # safe.
        for token in payload.split(", "):
            if "=" in token:
                key, _, value = token.partition("=")
                record[key.strip()] = value.strip()
        if record:
            rows.append(record)
    return rows


def extract(device_id: str, staging_dir: Path, is_privileged: bool) -> list[MediaFile]:
    """
    Extract videos from an Android device.

    Parameters
    ----------
    device_id:
        The ADB serial of the target device.
    staging_dir:
        Local directory where pulled files are written.
    is_privileged:
        True if the device is rooted (not currently used but reserved for
        future root-based extraction paths).

    Returns
    -------
    list[MediaFile]
        One entry per successfully pulled video file.
    """
    staging_dir = Path(staging_dir)
    videos_dir = staging_dir / "videos_android"
    videos_dir.mkdir(parents=True, exist_ok=True)

    config = get_config()
    adb_path: str = str(config.adb_exe)

    # Query the MediaStore.
    result = _adb(
        device_id,
        "shell",
        "content",
        "query",
        "--uri",
        MEDIA_URI,
        "--projection",
        PROJECTION,
        adb_path=adb_path,
    )

    if result.returncode != 0:
        logger.error(
            "extract_videos_android: MediaStore query failed (rc=%d): %s",
            result.returncode,
            result.stderr.strip(),
        )
        return []

    rows = _parse_content_rows(result.stdout)
    if not rows:
        logger.info("extract_videos_android: no video entries found in MediaStore")
        return []

    logger.info("extract_videos_android: found %d MediaStore video entries", len(rows))

    results: list[MediaFile] = []
    for row in rows:
        remote_path = row.get("_data", "").strip()
        display_name = row.get("_display_name", "").strip()
        date_taken_str = row.get("date_taken", "").strip()
        date_added_str = row.get("date_added", "").strip()
        bucket = row.get("bucket_display_name", "videos").strip() or "videos"

        if not remote_path:
            logger.debug("extract_videos_android: skipping row with no _data: %s", row)
            continue
        if not display_name:
            display_name = Path(remote_path).name

        # Build a collision-safe local path.
        local_path = videos_dir / display_name
        if local_path.exists():
            stem = Path(display_name).stem
            suffix = Path(display_name).suffix
            counter = 1
            while local_path.exists():
                local_path = videos_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        pull = _adb(device_id, "pull", remote_path, str(local_path), adb_path=adb_path)
        if pull.returncode != 0:
            logger.warning(
                "extract_videos_android: failed to pull %s: %s",
                remote_path,
                pull.stderr.strip(),
            )
            continue

        # Prefer date_taken (actual recording time, milliseconds since epoch,
        # populated by MediaScanner from the MP4/MOV moov/mvhd creation_time);
        # fall back to date_added (Unix seconds) when date_taken is missing
        # or zero — common for videos copied in from elsewhere.
        created: datetime | None = None
        if date_taken_str and date_taken_str not in ("0", "null"):
            try:
                ts = int(date_taken_str) / 1000.0
                created = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, OSError):
                created = None
        if created is None and date_added_str.isdigit():
            created = datetime.fromtimestamp(int(date_added_str), tz=timezone.utc)

        mime_type, _ = mimetypes.guess_type(display_name)
        if mime_type is None or not mime_type.startswith("video/"):
            mime_type = "video/mp4"

        media_file = MediaFile(
            filename=display_name,
            mime_type=mime_type,
            local_path=local_path,
            created=created,
            album=bucket,
            latitude=None,
            longitude=None,
        )
        results.append(media_file)
        logger.debug("extract_videos_android: pulled %s", remote_path)

    logger.info(
        "extract_videos_android: extracted %d video(s) from device %s",
        len(results),
        device_id,
    )
    return results
