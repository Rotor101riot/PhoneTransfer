from __future__ import annotations

import json
import logging
import subprocess
from collections import defaultdict
from pathlib import Path

from core.config_loader import get_config
from core.normalization_schema import Bookmark

logger = logging.getLogger(__name__)

_CHROME_DB_DEVICE = (
    "/data/data/com.android.chrome/app_chrome/Default/Bookmarks"
)
_SDCARD_PULL = "/sdcard/PhoneTransfer_bm_pull.json"
_SDCARD_PUSH = "/sdcard/PhoneTransfer_bm_push.json"
_SDCARD_HTML = "/sdcard/PhoneTransfer/bookmarks.html"

# Windows FILETIME epoch offset in microseconds
_FILETIME_EPOCH_DIFF_US = 11_644_473_600 * 1_000_000


def _datetime_to_filetime(dt) -> str:
    """Convert a datetime to Chrome's Windows FILETIME microseconds string."""
    try:
        unix_us = int(dt.timestamp() * 1_000_000)
        return str(unix_us + _FILETIME_EPOCH_DIFF_US)
    except Exception:
        return "0"


# ---- Netscape HTML helpers ----

def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _datetime_to_add_date(dt) -> str:
    try:
        return str(int(dt.timestamp()))
    except Exception:
        return "0"


def _build_netscape_html(items: list[Bookmark]) -> str:
    grouped: dict[str | None, list[Bookmark]] = defaultdict(list)
    for bm in items:
        grouped[bm.folder].append(bm)

    lines = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        "<!-- This is an automatically generated file. -->",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        "<TITLE>Bookmarks</TITLE>",
        "<H1>Bookmarks</H1>",
        "<DL><p>",
        "    <DT><H3>Imported from PhoneTransfer</H3>",
        "    <DL><p>",
    ]
    for bm in grouped.get(None, []):
        add_date = _datetime_to_add_date(bm.added) if bm.added else "0"
        lines.append(
            f'        <DT><A HREF="{_escape_html(bm.url)}" ADD_DATE="{add_date}">'
            f"{_escape_html(bm.title or bm.url)}</A>"
        )
    for folder, bms in grouped.items():
        if folder is None:
            continue
        lines.append(f"        <DT><H3>{_escape_html(folder)}</H3>")
        lines.append("        <DL><p>")
        for bm in bms:
            add_date = _datetime_to_add_date(bm.added) if bm.added else "0"
            lines.append(
                f'            <DT><A HREF="{_escape_html(bm.url)}" ADD_DATE="{add_date}">'
                f"{_escape_html(bm.title or bm.url)}</A>"
            )
        lines.append("        </DL><p>")
    lines += ["    </DL><p>", "</DL><p>"]
    return "\n".join(lines) + "\n"


# ---- ADB helpers ----

def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def _push_html_fallback(
    adb: str, device_id: str, items: list[Bookmark], staging_dir: Path
) -> int:
    """Generate and push a Netscape bookmark HTML file to /sdcard/."""
    local_html = staging_dir / "bookmarks.html"
    try:
        local_html.write_text(_build_netscape_html(items), encoding="utf-8")
        _run([adb, "-s", device_id, "shell", "mkdir", "-p", "/sdcard/PhoneTransfer"])
        result = _run([adb, "-s", device_id, "push", str(local_html), _SDCARD_HTML])
        if result.returncode == 0:
            logger.info(
                "Pushed bookmarks.html to %s on device %s. "
                "Import via Chrome: chrome://bookmarks → Import bookmarks.",
                _SDCARD_HTML,
                device_id,
            )
            return len(items)
        else:
            logger.error(
                "Failed to push bookmarks HTML to device %s: %s",
                device_id,
                result.stderr.strip(),
            )
            return 0
    except Exception as exc:
        logger.error("Exception during HTML push for device %s: %s", device_id, exc)
        return 0


# ---- Rooted merge ----

def _bookmark_to_chrome_node(bm: Bookmark) -> dict:
    node: dict = {
        "type": "url",
        "name": bm.title or bm.url,
        "url": bm.url,
    }
    if bm.added:
        node["date_added"] = _datetime_to_filetime(bm.added)
    return node


def _merge_rooted(
    adb: str, device_id: str, items: list[Bookmark], staging_dir: Path
) -> int:
    """Read Chrome's Bookmarks JSON, merge new bookmarks, write back via root."""
    local_pull = staging_dir / "chrome_bm_pull.json"
    local_push = staging_dir / "chrome_bm_push.json"

    # Pull existing bookmarks
    try:
        cp_result = _run(
            [
                adb, "-s", device_id, "shell", "su", "-c",
                f"cp {_CHROME_DB_DEVICE} {_SDCARD_PULL} && chmod 644 {_SDCARD_PULL}",
            ]
        )
        if cp_result.returncode != 0:
            logger.warning(
                "Root cp of Chrome Bookmarks failed for device %s: %s",
                device_id,
                cp_result.stderr.strip(),
            )
            return 0
        pull_result = _run(
            [adb, "-s", device_id, "pull", _SDCARD_PULL, str(local_pull)]
        )
        if pull_result.returncode != 0:
            logger.warning(
                "adb pull of Chrome Bookmarks failed for device %s: %s",
                device_id,
                pull_result.stderr.strip(),
            )
            return 0
        data = json.loads(local_pull.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(
            "Could not retrieve existing Chrome Bookmarks for device %s: %s",
            device_id,
            exc,
        )
        return 0

    # Merge into roots.other.children
    try:
        roots = data.setdefault("roots", {})
        other = roots.setdefault("other", {"children": [], "name": "Other Bookmarks", "type": "folder"})
        other.setdefault("children", [])

        # Collect existing URLs to skip duplicates
        existing_urls: set[str] = set()
        for child in other["children"]:
            if child.get("type") == "url":
                existing_urls.add(child.get("url", ""))

        added = 0
        for bm in items:
            if bm.url in existing_urls:
                logger.debug("Skipping duplicate bookmark URL: %s", bm.url)
                continue
            other["children"].append(_bookmark_to_chrome_node(bm))
            existing_urls.add(bm.url)
            added += 1

        local_push.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.error("Failed to merge Chrome bookmarks JSON: %s", exc)
        return 0

    # Push merged JSON back
    try:
        push_result = _run(
            [adb, "-s", device_id, "push", str(local_push), _SDCARD_PUSH]
        )
        if push_result.returncode != 0:
            logger.error(
                "adb push of merged Chrome Bookmarks failed for device %s: %s",
                device_id,
                push_result.stderr.strip(),
            )
            return 0
        cp_back = _run(
            [
                adb, "-s", device_id, "shell", "su", "-c",
                f"cp {_SDCARD_PUSH} {_CHROME_DB_DEVICE}",
            ]
        )
        if cp_back.returncode != 0:
            logger.error(
                "Root cp-back of merged Chrome Bookmarks failed for device %s: %s",
                device_id,
                cp_back.stderr.strip(),
            )
            return 0
        logger.info(
            "Merged %d bookmark(s) into Chrome Bookmarks on device %s.",
            added,
            device_id,
        )
        return added
    except Exception as exc:
        logger.error(
            "Exception pushing merged Chrome Bookmarks to device %s: %s",
            device_id,
            exc,
        )
        return 0


def inject(
    device_id: str, items: list[Bookmark], staging_dir: Path, is_privileged: bool
) -> int:
    """Inject bookmarks into Android Chrome.

    Rooted: merges bookmarks into Chrome's Bookmarks JSON directly.
    Non-rooted: generates a Netscape bookmark HTML file and pushes it to
    /sdcard/PhoneTransfer/bookmarks.html for manual import via Chrome.

    Args:
        device_id: ADB serial number.
        items: Bookmark objects to inject.
        staging_dir: Local directory for temporary files.
        is_privileged: True if root access is available.

    Returns:
        Number of bookmarks added.
    """
    if not items:
        logger.info("No bookmarks to inject for device %s.", device_id)
        return 0

    staging_dir.mkdir(parents=True, exist_ok=True)
    cfg = get_config()
    adb = cfg.adb_exe

    if is_privileged:
        count = _merge_rooted(adb, device_id, items, staging_dir)
        if count > 0:
            return count
        logger.warning(
            "Rooted merge failed for device %s; falling back to HTML export.",
            device_id,
        )

    # Non-rooted path (or rooted fallback)
    logger.info(
        "Non-rooted path: generating Netscape bookmark HTML for device %s. "
        "Import it via Chrome: chrome://bookmarks → Import bookmarks.",
        device_id,
    )
    return _push_html_fallback(adb, device_id, items, staging_dir)
