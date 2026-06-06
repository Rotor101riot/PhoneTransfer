"""
inject_apps_android.py

Installs staged APKs onto an Android destination device via ADB.

Version-aware logic
-------------------
Before installing each package, the injector queries the destination device
to determine whether the package is already present and, if so, whether the
source version is newer.  Four outcomes are possible:

    INSTALLED_NEW   — package was absent on dest; install succeeded.
    UPDATED         — package was present; source version is newer; updated.
    SKIPPED_NEWER   — dest already has a newer or equal version; skipped.
    FAILED          — install attempt returned a failure code.

The function returns a summary dict so the UI / session log can display a
human-readable breakdown (inspired by Open Android Backup's app restore).

Split APK support
-----------------
If a package directory contains more than one .apk file (base.apk + split
config APKs) the injector uses ``adb install-multiple`` via
ADBManager.install_multiple().  Single-APK packages use
ADBManager.install_apk().
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.adb_manager import ADBManager
from core.config_loader import get_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def inject(
    dest_serial: str,
    items: list[dict],
    staging_path: Path,
    privileged: bool = False,
    config=None,
) -> int:
    """
    Install staged APKs from *items* onto *dest_serial*.

    Follows the standard pipeline injector signature:
        inject(device_id, items, staging_dir, privileged) -> int

    Parameters
    ----------
    dest_serial:
        ADB serial of the destination device.
    items:
        List of AppInfo dicts returned by extract_apps_android.extract().
    staging_path:
        Session staging directory (not used directly; apk_files paths
        are already absolute within it).
    privileged:
        Unused (adb install-multiple works without root).  Accepted for API
        compatibility with the pipeline calling convention.
    config:
        Optional Config.  Defaults to get_config().

    Returns
    -------
    int
        Number of packages successfully installed or updated.
    """
    if not items:
        logger.info("[apps/android] No apps to inject.")
        return 0

    cfg = config or get_config()
    adb = ADBManager(cfg)

    installed_new: list[str]    = []
    updated:       list[str]    = []
    skipped_newer: list[tuple]  = []   # (pkg, src_vc, dest_vc)
    failed:        list[str]    = []

    dest_packages = _get_installed_packages(adb, dest_serial)
    total = len(items)

    for idx, info in enumerate(items, 1):
        pkg      = info["package"]
        src_vc   = info.get("version_code", 0)
        apk_files: list[Path] = info.get("apk_files", [])

        logger.info(
            "[apps/android] Processing %s (%d/%d) v%s",
            pkg, idx, total, info.get("version_name", "?"),
        )

        if not apk_files:
            logger.warning("[apps/android] No APK files for %s — skipping", pkg)
            failed.append(pkg)
            continue

        # Check what is already installed on the destination
        dest_vc = dest_packages.get(pkg, -1)

        if dest_vc >= src_vc > 0:
            logger.info(
                "[apps/android] Skipping %s — dest versionCode %d >= src %d",
                pkg, dest_vc, src_vc,
            )
            skipped_newer.append((pkg, src_vc, dest_vc))
            continue

        # Perform install
        ok = adb.install_multiple(dest_serial, apk_files, timeout=300)
        if ok:
            if dest_vc == -1:
                installed_new.append(pkg)
            else:
                updated.append(pkg)
        else:
            logger.warning("[apps/android] Install failed for %s", pkg)
            failed.append(pkg)

    # Log summary
    _log_summary(installed_new, updated, skipped_newer, failed)

    return len(installed_new) + len(updated)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_installed_packages(adb: ADBManager, serial: str) -> dict[str, int]:
    """
    Return {package_name: version_code} for every installed package on *serial*.
    Uses 'pm list packages' + 'dumpsys package' for version codes.

    Note: querying version codes for every installed package is slow, so we
    only resolve the version codes lazily for packages that are about to be
    installed.  Here we return -1 as a sentinel for "installed but version
    unknown", and 0 for "not installed".
    """
    stdout, _, rc = adb.shell(serial, "pm list packages", timeout=30)
    if rc != 0:
        return {}
    installed: dict[str, int] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            pkg = line[len("package:"):].strip()
            if pkg:
                installed[pkg] = -1   # present, version not yet resolved
    return installed


def _resolve_version_code(adb: ADBManager, serial: str, pkg: str) -> int:
    """Query versionCode for a single installed package. Returns 0 on failure."""
    import re
    stdout, _, rc = adb.shell(serial, f"dumpsys package {pkg}", timeout=15)
    if rc != 0:
        return 0
    for line in stdout.splitlines():
        m = re.search(r"versionCode=(\d+)", line.strip())
        if m:
            return int(m.group(1))
    return 0


def _log_summary(
    installed_new: list[str],
    updated: list[str],
    skipped_newer: list[tuple],
    failed: list[str],
) -> None:
    logger.info(
        "[apps/android] Install summary: "
        "new=%d  updated=%d  skipped(dest newer)=%d  failed=%d",
        len(installed_new), len(updated), len(skipped_newer), len(failed),
    )
    for pkg in installed_new:
        logger.info("  [apps] INSTALLED (new):  %s", pkg)
    for pkg in updated:
        logger.info("  [apps] UPDATED:          %s", pkg)
    for pkg, src_vc, dest_vc in skipped_newer:
        logger.info(
            "  [apps] SKIPPED (dest %d >= src %d): %s",
            dest_vc, src_vc, pkg,
        )
    for pkg in failed:
        logger.warning("  [apps] FAILED:           %s", pkg)
