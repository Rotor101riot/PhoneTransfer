from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from core.config_loader import get_config
from core.normalization_schema import Bookmark

logger = logging.getLogger(__name__)

_CHROME_DB_PATH = "/data/data/com.android.chrome/app_chrome/Default/Bookmarks"
_SDCARD_TEMP = "/sdcard/PhoneTransfer_bookmarks.json"

# Windows FILETIME epoch is 1601-01-01; Unix epoch is 1970-01-01.
_FILETIME_EPOCH_DIFF = 11_644_473_600  # seconds


def _filetime_to_datetime(filetime_micros: int | str | None) -> datetime | None:
    """Convert a Chrome Windows FILETIME (microseconds since 1601-01-01) to datetime."""
    if filetime_micros is None:
        return None
    try:
        micros = int(filetime_micros)
        unix_ts = micros / 1_000_000 - _FILETIME_EPOCH_DIFF
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    except Exception:
        return None


def _walk_chrome_node(
    node: dict, folder: str | None, bookmarks: list[Bookmark]
) -> None:
    """Recursively walk a Chrome bookmark tree node, appending Bookmarks."""
    node_type = node.get("type", "")
    name = node.get("name", "")

    if node_type == "url":
        url = node.get("url", "")
        if url:
            added = _filetime_to_datetime(node.get("date_added"))
            bookmarks.append(
                Bookmark(
                    title=name or url,
                    url=url,
                    folder=folder,
                    added=added,
                )
            )
    elif node_type == "folder":
        children = node.get("children", [])
        for child in children:
            _walk_chrome_node(child, folder=name or folder, bookmarks=bookmarks)
    else:
        # Unknown node type — still recurse if children present
        for child in node.get("children", []):
            _walk_chrome_node(child, folder=folder, bookmarks=bookmarks)


def _parse_chrome_bookmarks_json(json_path: Path) -> list[Bookmark]:
    """Parse a Chrome Bookmarks JSON file and return Bookmark objects."""
    bookmarks: list[Bookmark] = []
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        roots = data.get("roots", {})
        for root_name, root_node in roots.items():
            if isinstance(root_node, dict):
                _walk_chrome_node(root_node, folder=None, bookmarks=bookmarks)
        logger.info(
            "Parsed %d Chrome bookmark(s) from %s", len(bookmarks), json_path
        )
    except Exception as exc:
        logger.error("Failed to parse Chrome Bookmarks JSON at %s: %s", json_path, exc)
    return bookmarks


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def extract(device_id: str, staging_dir: Path, is_privileged: bool) -> list[Bookmark]:
    """Extract Chrome bookmarks from an Android device.

    Requires root access to copy the Bookmarks file out of Chrome's sandbox.
    Without root, logs the limitation and returns [].

    Args:
        device_id: ADB serial number.
        staging_dir: Local directory for temporary files.
        is_privileged: True if root access is available.

    Returns:
        A list of Bookmark objects, or [] on failure.
    """
    if not is_privileged:
        logger.warning(
            "Cannot extract Chrome bookmarks from device %s without root access. "
            "Chrome's bookmark file is sandboxed and inaccessible to non-root callers.",
            device_id,
        )
        return []

    staging_dir.mkdir(parents=True, exist_ok=True)
    cfg = get_config()
    adb = cfg.adb_exe
    local_json = staging_dir / "chrome_bookmarks.json"

    # Copy bookmark file to sdcard via root
    try:
        _run([adb, "-s", device_id, "shell", "mkdir", "-p", "/sdcard/PhoneTransfer"])
        cp_result = _run(
            [
                adb,
                "-s",
                device_id,
                "shell",
                "su",
                "-c",
                f"cp {_CHROME_DB_PATH} {_SDCARD_TEMP}",
            ]
        )
        if cp_result.returncode != 0:
            logger.error(
                "Root cp of Chrome Bookmarks failed for device %s: %s",
                device_id,
                cp_result.stderr.strip(),
            )
            return []
    except Exception as exc:
        logger.error(
            "Exception during root cp of Chrome Bookmarks for device %s: %s",
            device_id,
            exc,
        )
        return []

    # Pull the file to local staging
    try:
        pull_result = _run(
            [adb, "-s", device_id, "pull", _SDCARD_TEMP, str(local_json)]
        )
        if pull_result.returncode != 0:
            logger.error(
                "adb pull of Chrome Bookmarks failed for device %s: %s",
                device_id,
                pull_result.stderr.strip(),
            )
            return []
        logger.info(
            "Pulled Chrome Bookmarks JSON to %s for device %s", local_json, device_id
        )
    except Exception as exc:
        logger.error(
            "Exception during adb pull of Chrome Bookmarks for device %s: %s",
            device_id,
            exc,
        )
        return []

    return _parse_chrome_bookmarks_json(local_json)
