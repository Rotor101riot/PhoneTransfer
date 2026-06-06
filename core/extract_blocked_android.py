from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from core.models import BlockedNumber

logger = logging.getLogger(__name__)

# Content provider URIs tried in order for non-root path.
_PROVIDER_URIS = [
    "content://call_log/call_log_blocked",
    "content://com.android.providers.telephony.BlockedNumberProvider/blocked",
]

# Regex to parse a single field from an ADB content-query row.
# Row format: "Row: 0 _id=1, original_number=+15551234567, e164_number=+15551234567"
_ROW_RE = re.compile(r"Row:\s*\d+\s+(.+)")
_FIELD_RE = re.compile(r"(\w+)=([^,]+)")


def extract(device_id: str, staging_dir: Path, is_rooted: bool) -> list[BlockedNumber]:
    """Extract blocked phone numbers from an Android device.

    Tries the BlockedNumbers content provider first (API 24+).
    Falls back to direct SQLite query via root shell if the provider is unavailable.
    """
    results = _extract_via_content_provider(device_id)
    if results is not None:
        logger.info(
            "extract_blocked_android: extracted %d blocked numbers via content provider",
            len(results),
        )
        return results

    if is_rooted:
        logger.info(
            "extract_blocked_android: content provider unavailable; "
            "falling back to root SQLite query (device_id=%s)",
            device_id,
        )
        results = _extract_via_root_sqlite(device_id)
        if results is not None:
            logger.info(
                "extract_blocked_android: extracted %d blocked numbers via root SQLite",
                len(results),
            )
            return results

    logger.warning(
        "extract_blocked_android: could not extract blocked numbers from device %s. "
        "The device may be running Android < 7.0 or the caller lacks READ_BLOCKED_NUMBERS "
        "permission. Root access would allow a direct database query.",
        device_id,
    )
    return []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_adb(device_id: str, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess | None:
    """Run an adb command and return the CompletedProcess, or None on error."""
    cmd = ["adb", "-s", device_id] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result
    except FileNotFoundError:
        logger.error("extract_blocked_android: 'adb' executable not found in PATH")
        return None
    except subprocess.TimeoutExpired:
        logger.error("extract_blocked_android: adb command timed out: %s", " ".join(cmd))
        return None
    except Exception as exc:
        logger.error("extract_blocked_android: unexpected error running adb: %s", exc)
        return None


def _parse_content_query_output(output: str) -> list[BlockedNumber]:
    """Parse the text output of `adb shell content query` into BlockedNumber objects."""
    results: list[BlockedNumber] = []
    for line in output.splitlines():
        line = line.strip()
        row_match = _ROW_RE.match(line)
        if not row_match:
            continue
        fields_str = row_match.group(1)
        fields: dict[str, str] = {}
        for field_match in _FIELD_RE.finditer(fields_str):
            fields[field_match.group(1).strip()] = field_match.group(2).strip()

        # Prefer e164_number for canonical form; fall back to original_number.
        number = fields.get("e164_number") or fields.get("original_number")
        if not number:
            logger.debug(
                "extract_blocked_android: row had no recognisable number field: %s", line
            )
            continue

        results.append(BlockedNumber(number=number))
    return results


def _extract_via_content_provider(device_id: str) -> list[BlockedNumber] | None:
    """Try each content provider URI in turn.

    Returns a list (possibly empty) on success, or None if all URIs failed.
    """
    for uri in _PROVIDER_URIS:
        result = _run_adb(device_id, ["shell", "content", "query", "--uri", uri])
        if result is None:
            # adb itself is broken; no point retrying.
            return None

        stderr_lower = result.stderr.lower()
        if result.returncode != 0 or "exception" in stderr_lower or "error" in stderr_lower:
            logger.debug(
                "extract_blocked_android: URI %s failed (rc=%d): %s",
                uri,
                result.returncode,
                (result.stderr or result.stdout).strip(),
            )
            continue

        stdout = result.stdout.strip()

        # "No result found." means the provider is accessible but the table is empty.
        if "no result found" in stdout.lower():
            logger.debug(
                "extract_blocked_android: URI %s accessible but table is empty", uri
            )
            return []

        parsed = _parse_content_query_output(stdout)
        logger.debug(
            "extract_blocked_android: URI %s returned %d rows", uri, len(parsed)
        )
        return parsed

    return None


def _extract_via_root_sqlite(device_id: str) -> list[BlockedNumber] | None:
    """Query the BlockedNumbers SQLite database directly using root shell."""
    db_path = "/data/data/com.android.providers.telephony/databases/blocked.db"
    sql = "SELECT original_number FROM blocked"
    shell_cmd = f"su -c 'sqlite3 {db_path} \"{sql}\"'"

    result = _run_adb(device_id, ["shell", shell_cmd])
    if result is None:
        return None

    if result.returncode != 0:
        logger.debug(
            "extract_blocked_android: root SQLite query failed (rc=%d): %s",
            result.returncode,
            result.stderr.strip(),
        )
        return None

    results: list[BlockedNumber] = []
    for line in result.stdout.splitlines():
        number = line.strip()
        if number:
            results.append(BlockedNumber(number=number))
    return results
