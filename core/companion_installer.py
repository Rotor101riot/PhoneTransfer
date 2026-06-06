"""
core/companion_installer.py
===========================
Handles detecting, installing, and updating the PhoneTransfer companion APK
on a connected Android device.

Public API
----------
    check_status(serial, adb)  -> (CompanionStatus, str)
    install_companion(serial, adb) -> (bool, str)
    find_apk()                 -> Path | None
    load_meta()                -> dict | None

Versioning
----------
The bundled APK is accompanied by  assets/companion_meta.json:

    {
      "package":      "com.phonetransfer.companion.debug",
      "version_code": 1,
      "variant":      "debug"
    }

The installer uses this to know *which* package to look for on the device and
whether a newer version is available.  Use  scripts/copy_apk.py  to update
both files after every Android build.

Design notes
------------
- All ADB calls go through the existing ADBManager interface so error handling
  and logging are consistent with the rest of the pipeline.
- PyInstaller: assets/ is expected at sys._MEIPASS/assets/ when frozen.
- Never raises — all failure paths return (False, error_msg) or log + return None.
"""

from __future__ import annotations

import json
import logging
import sys
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.adb_manager import ADBManager

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_COMPANION_META_FILE = "companion_meta.json"
_COMPANION_APK_FILE  = "companion.apk"
_PACKAGE_BASE        = "com.phonetransfer.companion"


# ── Status enum ───────────────────────────────────────────────────────────────

class CompanionStatus(Enum):
    APK_MISSING       = "apk_missing"      # assets/companion.apk not found
    NOT_INSTALLED     = "not_installed"    # package absent from device
    UPDATE_AVAILABLE  = "update"           # older version installed
    UP_TO_DATE        = "up_to_date"       # current version installed


# ── Asset helpers ─────────────────────────────────────────────────────────────

def _assets_dir() -> Path:
    """
    Return the assets/ directory.

    Handles both normal execution (assets/ next to the repo root) and
    PyInstaller frozen bundles (sys._MEIPASS/assets/).
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "assets"  # type: ignore[attr-defined]
    # __file__ is  core/companion_installer.py  → go up one level
    return Path(__file__).parent.parent / "assets"


def load_meta() -> dict | None:
    """
    Load and return companion_meta.json, or None if missing / invalid.
    Expected keys: package (str), version_code (int), variant (str).
    """
    path = _assets_dir() / _COMPANION_META_FILE
    if not path.exists():
        logger.debug("companion_meta.json not found at %s", path)
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read companion_meta.json: %s", exc)
        return None


def find_apk() -> Path | None:
    """Return the path to the bundled companion.apk, or None if absent."""
    apk = _assets_dir() / _COMPANION_APK_FILE
    return apk if apk.exists() else None


# ── Device queries ────────────────────────────────────────────────────────────

def get_installed_version(serial: str, adb: "ADBManager", package: str) -> int | None:
    """
    Return the versionCode of *package* installed on *serial*, or None if the
    package is not installed or the query fails.

    Uses ``dumpsys package`` which is available on all API levels ≥ 26.
    """
    stdout, _, rc = adb.shell(
        serial,
        f"dumpsys package {package} 2>/dev/null | grep 'versionCode='",
        timeout=15,
    )
    if rc != 0 or not stdout.strip():
        logger.debug(
            "get_installed_version: package %s not found on %s", package, serial
        )
        return None

    # Line looks like: "    versionCode=1 minSdk=26 targetSdk=34"
    for token in stdout.split():
        if token.startswith("versionCode="):
            try:
                return int(token.split("=", 1)[1])
            except (ValueError, IndexError):
                logger.warning("Unexpected versionCode token: %r", token)
                return None
    return None


def is_package_installed(serial: str, adb: "ADBManager", package: str) -> bool:
    """
    Fast check — returns True if *package* appears in  pm list packages.
    Cheaper than  dumpsys  when we just need presence, not version.
    """
    stdout, _, rc = adb.shell(
        serial,
        f"pm list packages {package} 2>/dev/null",
        timeout=10,
    )
    return rc == 0 and f"package:{package}" in stdout


# ── Status check ─────────────────────────────────────────────────────────────

def check_status(serial: str, adb: "ADBManager") -> tuple[CompanionStatus, str]:
    """
    Determine the companion install status on *serial*.

    Returns a (CompanionStatus, human_readable_message) tuple.
    Never raises.
    """
    meta = load_meta()
    if meta is None or not find_apk():
        return (
            CompanionStatus.APK_MISSING,
            "Companion APK not bundled — run  scripts/copy_apk.py",
        )

    package       = meta.get("package", _PACKAGE_BASE)
    bundled_ver   = int(meta.get("version_code", 1))

    installed_ver = get_installed_version(serial, adb, package)

    if installed_ver is None:
        return CompanionStatus.NOT_INSTALLED, "Companion app not installed"

    if installed_ver < bundled_ver:
        return (
            CompanionStatus.UPDATE_AVAILABLE,
            f"Update available (installed v{installed_ver}, bundled v{bundled_ver})",
        )

    return (
        CompanionStatus.UP_TO_DATE,
        f"Companion v{installed_ver} installed ✓",
    )


# ── Installation ──────────────────────────────────────────────────────────────

def install_companion(serial: str, adb: "ADBManager") -> tuple[bool, str]:
    """
    Sideload the bundled companion APK onto *serial*.

    Returns (success: bool, message: str).
    The ADB install uses -r (replace) and -d (allow downgrade) flags so it
    works for both fresh installs and updates.

    Prerequisites
    -------------
    - USB debugging must be enabled on the device.
    - The device must already be authorised (status == "device" in adb devices).
    - The bundled APK must exist (check find_apk() / load_meta() first).
    """
    apk = find_apk()
    if apk is None:
        msg = "companion.apk not found — run  scripts/copy_apk.py  first"
        logger.error(msg)
        return False, msg

    meta    = load_meta() or {}
    package = meta.get("package", _PACKAGE_BASE)
    variant = meta.get("variant", "unknown")
    logger.info(
        "Installing companion APK (%s, package=%s) on %s ...",
        variant, package, serial,
    )

    ok = adb.install_apk(serial, apk, timeout=120)
    if ok:
        msg = f"Companion installed successfully (package: {package})"
        logger.info(msg)
        return True, msg

    msg = (
        "Installation failed — ensure USB debugging is enabled and "
        "the device is fully authorised in the ADB prompt."
    )
    logger.error(msg)
    return False, msg


# ── Convenience ───────────────────────────────────────────────────────────────

def ensure_companion(serial: str, adb: "ADBManager") -> tuple[bool, str]:
    """
    Install or update the companion if needed.

    - Returns (True, msg)  if already up to date or install succeeded.
    - Returns (False, msg) if the APK is missing or install failed.
    """
    status, msg = check_status(serial, adb)

    if status == CompanionStatus.UP_TO_DATE:
        return True, msg

    if status == CompanionStatus.APK_MISSING:
        return False, msg

    # NOT_INSTALLED or UPDATE_AVAILABLE → try to install
    return install_companion(serial, adb)
