"""
inject_browser_android.py

Injects browser history entries into Chrome on an Android device.

Requires root access — Chrome's History SQLite is in a protected app directory.

Procedure (rooted):
  1. Force-stop Chrome.
  2. Root-copy History DB to /sdcard/, pull it locally.
  3. Insert new entries into the urls and visits tables.
  4. Push the modified DB back and restore ownership.
  5. Start Chrome.

Non-rooted: logs guidance and returns 0.

Returns count of entries successfully injected.
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from core.config_loader import get_config
from core.normalization_schema import BrowserHistoryEntry

logger = logging.getLogger(__name__)

_CHROME_PKG    = "com.android.chrome"
_HISTORY_DEV   = "/data/data/com.android.chrome/app_chrome/Default/History"
_SDCARD_PULL   = "/sdcard/PT_chrome_hist_pull"
_SDCARD_PUSH   = "/sdcard/PT_chrome_hist_push"

# Chromium timestamps: microseconds since 1601-01-01 00:00:00 UTC
_CHROMIUM_EPOCH_US = 11644473600 * 1_000_000   # Unix epoch as Chromium μs


def inject(
    device_id: str,
    items: list[BrowserHistoryEntry],
    staging_dir: Path,
    is_privileged: bool,
) -> int:
    if not items:
        return 0

    if not is_privileged:
        logger.warning(
            "[browser/android] Browser history injection requires root. "
            "Chrome's History database is sandboxed in /data/data/. "
            "%d entries are available in the vault for future use.",
            len(items),
        )
        return 0

    try:
        return _inject_impl(device_id, items, staging_dir)
    except Exception:
        logger.exception("[browser/android] Unhandled error during injection")
        return 0


def _inject_impl(
    device_id: str,
    items: list[BrowserHistoryEntry],
    staging_dir: Path,
) -> int:
    staging_dir.mkdir(parents=True, exist_ok=True)
    cfg = get_config()
    adb = cfg.adb_exe
    local_db = staging_dir / "chrome_history_inject.db"

    # 1. Force-stop Chrome
    _run([adb, "-s", device_id, "shell", "su", "-c", f"am force-stop {_CHROME_PKG}"])

    # 2. Pull History DB
    rc = _run([adb, "-s", device_id, "shell", "su", "-c",
               f"cp {_HISTORY_DEV} {_SDCARD_PULL} && chmod 644 {_SDCARD_PULL}"]).returncode
    if rc != 0:
        logger.error(
            "[browser/android] Could not copy Chrome History to /sdcard/ on %s. "
            "Chrome may not be installed or the path differs on this device version.",
            device_id,
        )
        return 0

    pull = _run([adb, "-s", device_id, "pull", _SDCARD_PULL, str(local_db)])
    _run([adb, "-s", device_id, "shell", f"rm -f {_SDCARD_PULL}"])

    if pull.returncode != 0 or not local_db.exists():
        logger.error("[browser/android] adb pull of Chrome History failed on %s", device_id)
        return 0

    # 3. Insert entries
    inserted = _insert_entries(local_db, items)
    if inserted == 0:
        return 0

    # 4. Push back
    push = _run([adb, "-s", device_id, "push", str(local_db), _SDCARD_PUSH])
    if push.returncode != 0:
        logger.error("[browser/android] adb push of modified History failed on %s", device_id)
        return 0

    # Restore ownership
    uid_result = _run([adb, "-s", device_id, "shell", "su", "-c",
                       f"stat -c %u /data/data/{_CHROME_PKG}"])
    chown = ""
    if uid_result.returncode == 0:
        uid = uid_result.stdout.strip()
        if uid.isdigit():
            chown = f" && chown {uid}:{uid} {_HISTORY_DEV}"

    rc = _run([adb, "-s", device_id, "shell", "su", "-c",
               f"cp {_SDCARD_PUSH} {_HISTORY_DEV}"
               f" && chmod 660 {_HISTORY_DEV}"
               f"{chown}"
               f" && rm -f {_SDCARD_PUSH}"]).returncode

    if rc != 0:
        logger.error("[browser/android] Failed to restore Chrome History in-place on %s", device_id)
        return 0

    # 5. Start Chrome
    _run([adb, "-s", device_id, "shell", "su", "-c",
          f"monkey -p {_CHROME_PKG} 1"], timeout=10)

    logger.info(
        "[browser/android] Injected %d Chrome history entries into %s",
        inserted, device_id,
    )
    return inserted


def _insert_entries(db_path: Path, items: list[BrowserHistoryEntry]) -> int:
    inserted = 0
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row

            # Build existing URL set to avoid duplicates
            existing: set[str] = set()
            try:
                for row in conn.execute("SELECT url FROM urls"):
                    existing.add(row["url"])
            except sqlite3.OperationalError:
                pass

            for entry in items:
                if not entry.url or entry.url in existing:
                    continue
                ts_us = _to_chromium_ts(entry.visited)
                try:
                    c = conn.execute(
                        "INSERT INTO urls (url, title, visit_count, last_visit_time, "
                        "hidden, typed_count) VALUES (?, ?, ?, ?, 0, 0)",
                        (entry.url, entry.title, entry.visit_count, ts_us),
                    )
                    url_id = c.lastrowid
                    conn.execute(
                        "INSERT INTO visits (url, visit_time, from_visit, transition, "
                        "segment_id, visit_duration) VALUES (?, ?, 0, 805306368, 0, 0)",
                        (url_id, ts_us),
                    )
                    existing.add(entry.url)
                    inserted += 1
                except Exception as exc:
                    logger.debug("[browser/android] Skipping %s: %s", entry.url[:60], exc)

            conn.commit()
    except Exception as exc:
        logger.error("[browser/android] DB insert error: %s", exc)

    return inserted


def _to_chromium_ts(dt: datetime) -> int:
    """Convert UTC datetime to Chromium microseconds (since 1601-01-01)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    unix_us = int(dt.timestamp() * 1_000_000)
    return unix_us + _CHROMIUM_EPOCH_US


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")
