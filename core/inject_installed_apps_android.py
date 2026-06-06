"""
inject_installed_apps_android.py

Injects installed apps into an Android device via the companion APK's
``inject_installed_apps`` command.

The companion APK acknowledges receipt of the app list but notes that
actual APK installation requires user action (via Play Store or manual
sideloading).

If APK files are available in the InstalledApp objects, falls back to
the existing ADB-based sideloading path (inject_apps_android).
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.companion_app_protocol import CompanionClient, setup_adb_forward, teardown_adb_forward
from core.config_loader import get_config
from core.normalization_schema import InstalledApp

logger = logging.getLogger(__name__)


def inject(
    serial: str,
    items: list[InstalledApp],
    staging_dir: Path,
    is_rooted: bool = False,
) -> int:
    """
    Inject installed apps into the Android device.

    Sends the app list to the companion APK for acknowledgement. If APK
    files are available, attempts ADB-based sideloading as well.

    Parameters
    ----------
    serial:      ADB device serial.
    items:       InstalledApp objects to inject.
    staging_dir: Local directory for temporary files.
    is_rooted:   Unused.

    Returns
    -------
    Number of apps successfully handled (acknowledged + sideloaded).
    """
    if not items:
        logger.info("[installed_apps/android] No apps to inject.")
        return 0

    # Check if any items have APK files available for sideloading
    items_with_apk = [i for i in items if i.apk_local_path and i.apk_local_path.exists()]

    if items_with_apk:
        # Use existing ADB-based sideloading for items with APK files
        return _sideload_via_adb(serial, items_with_apk, staging_dir, is_rooted)

    # No APK files — just send metadata to companion for acknowledgement
    try:
        from core.adb_manager import ADBManager
        adb = ADBManager(get_config())
        setup_adb_forward(adb, serial)
    except Exception as exc:
        logger.error("[installed_apps/android] ADB forward setup failed: %s", exc)
        return 0

    try:
        with CompanionClient() as client:
            if not client.ping():
                logger.warning(
                    "[installed_apps/android] Companion APK not responding on %s",
                    serial,
                )
                return 0

            data = []
            for app in items:
                data.append({
                    "package_name": app.package_name,
                    "app_name": app.app_name,
                    "version_name": app.version_name,
                    "version_code": app.version_code,
                })

            response = client.send_recv({
                "cmd": "inject_installed_apps",
                "data": data,
            })

        if response.get("status") != "ok":
            logger.warning(
                "[installed_apps/android] APK inject returned status '%s': %s",
                response.get("status"), response.get("message"),
            )
            return 0

        received = int(response.get("received", 0))
        note = response.get("note", "")
        if note:
            logger.info("[installed_apps/android] Companion note: %s", note)

        logger.info(
            "[installed_apps/android] Companion acknowledged %d app(s) on %s",
            received, serial,
        )
        return received

    except Exception:
        logger.exception("[installed_apps/android] Unhandled error during injection")
        return 0
    finally:
        try:
            teardown_adb_forward(adb, serial)
        except Exception:
            pass


def _sideload_via_adb(
    serial: str,
    items: list[InstalledApp],
    staging_dir: Path,
    is_rooted: bool,
) -> int:
    """
    Convert InstalledApp items (with APK files) to the AppInfo dict format
    expected by inject_apps_android and delegate to the ADB sideloader.
    """
    try:
        from core.inject_apps_android import inject as adb_inject
    except ImportError:
        logger.warning(
            "[installed_apps/android] inject_apps_android module not available "
            "for ADB sideloading"
        )
        return 0

    # Convert InstalledApp objects to the AppInfo dict format
    adb_items: list[dict] = []
    for app in items:
        if app.apk_local_path and app.apk_local_path.exists():
            adb_items.append({
                "package": app.package_name,
                "version_code": app.version_code or 0,
                "version_name": app.version_name or "",
                "apk_files": [app.apk_local_path],
                "apk_size_mb": round(app.apk_size / 1_048_576, 1) if app.apk_size else 0,
            })

    if not adb_items:
        return 0

    logger.info(
        "[installed_apps/android] Sideloading %d app(s) via ADB on %s",
        len(adb_items), serial,
    )
    return adb_inject(serial, adb_items, staging_dir, is_rooted)
