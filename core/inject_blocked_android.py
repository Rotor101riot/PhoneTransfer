from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from core.models import BlockedNumber

logger = logging.getLogger(__name__)

# Content provider URIs tried in order per number insertion.
_PROVIDER_URIS = [
    "content://call_log/call_log_blocked",
    "content://com.android.providers.telephony.BlockedNumberProvider/blocked",
]


def inject(device_id: str, items: list[BlockedNumber], staging_dir: Path, is_rooted: bool) -> int:
    """Inject blocked numbers into an Android device via the BlockedNumbers content provider.

    Tries the primary URI first; falls back to the full provider URI on failure.
    Returns the count of successfully inserted entries.
    """
    if not items:
        logger.debug("inject_blocked_android: no items to inject")
        return 0

    # Probe which URI is functional before iterating all items.
    working_uri = _probe_uri(device_id)
    if working_uri is None:
        logger.error(
            "inject_blocked_android: no accessible BlockedNumbers content provider found "
            "on device %s. The device may be running Android < 7.0 or the WRITE_BLOCKED_NUMBERS "
            "permission is not granted to the ADB shell.",
            device_id,
        )
        return 0

    logger.debug("inject_blocked_android: using URI %s for insertion", working_uri)

    success_count = 0
    for item in items:
        if _insert_number(device_id, item, working_uri):
            success_count += 1

    logger.info(
        "inject_blocked_android: inserted %d / %d blocked number(s) into device %s",
        success_count,
        len(items),
        device_id,
    )
    return success_count


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_adb(device_id: str, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess | None:
    """Run an adb command and return the CompletedProcess, or None on fatal error."""
    cmd = ["adb", "-s", device_id] + args
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        logger.error("inject_blocked_android: 'adb' executable not found in PATH")
        return None
    except subprocess.TimeoutExpired:
        logger.error("inject_blocked_android: adb command timed out: %s", " ".join(cmd))
        return None
    except Exception as exc:
        logger.error("inject_blocked_android: unexpected error running adb: %s", exc)
        return None


def _uri_is_accessible(device_id: str, uri: str) -> bool:
    """Return True if a content query against the URI does not produce an error."""
    result = _run_adb(device_id, ["shell", "content", "query", "--uri", uri])
    if result is None:
        return False
    if result.returncode != 0:
        return False
    combined = (result.stdout + result.stderr).lower()
    # Presence of "exception" or "error" in output indicates the provider rejected the call.
    if "exception" in combined or "error" in combined:
        return False
    return True


def _probe_uri(device_id: str) -> str | None:
    """Return the first accessible provider URI, or None if none work."""
    for uri in _PROVIDER_URIS:
        if _uri_is_accessible(device_id, uri):
            logger.debug("inject_blocked_android: URI probe succeeded: %s", uri)
            return uri
        logger.debug("inject_blocked_android: URI probe failed: %s", uri)
    return None


def _insert_number(device_id: str, item: BlockedNumber, primary_uri: str) -> bool:
    """Insert a single BlockedNumber using the given URI.

    Falls back to the other URIs if the primary one returns a non-zero exit code.
    Returns True on success.
    """
    uris_to_try = [primary_uri] + [u for u in _PROVIDER_URIS if u != primary_uri]

    for uri in uris_to_try:
        result = _run_adb(
            device_id,
            [
                "shell",
                "content", "insert",
                "--uri", uri,
                "--bind", f"original_number:s:{item.number}",
            ],
        )
        if result is None:
            # adb is broken; abort entirely.
            return False

        combined = (result.stdout + result.stderr).lower()
        if result.returncode == 0 and "exception" not in combined and "error" not in combined:
            logger.debug(
                "inject_blocked_android: inserted %s via %s", item.number, uri
            )
            return True

        logger.debug(
            "inject_blocked_android: insert of %s via %s failed (rc=%d): %s",
            item.number,
            uri,
            result.returncode,
            (result.stderr or result.stdout).strip(),
        )

    logger.warning(
        "inject_blocked_android: could not insert blocked number %s on device %s "
        "after trying all URIs",
        item.number,
        device_id,
    )
    return False
