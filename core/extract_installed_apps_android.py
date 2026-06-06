"""
extract_installed_apps_android.py

Extracts the installed app list (and optionally APK files) from an Android
device via the companion APK's ``extract_installed_apps`` command.

Two modes:

1. **List only** (default): The companion APK queries PackageManager and
   returns a JSON array of app metadata (package_name, app_name,
   version_name, version_code, apk_size, install_time, update_time).

2. **With APK backup** (``include_apk=True``): After the JSON response,
   the companion streams each APK as a sequence of binary frames:
     a. JSON ``app_apk_chunk`` header (package_name, filename, size)
     b. N binary frames (512 KB each)
     c. JSON ``app_apk_done`` (package_name)

Falls back to the existing ADB-based extraction (extract_apps_android)
if the companion is unavailable.

Returns a list of InstalledApp objects (normalization_schema.py).
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.companion_app_protocol import CompanionClient, setup_adb_forward, teardown_adb_forward
from core.config_loader import get_config
from core.normalization_schema import InstalledApp

logger = logging.getLogger(__name__)


def extract(
    serial: str,
    staging_dir: Path,
    is_rooted: bool = False,
    include_apk: bool = False,
) -> list[InstalledApp]:
    """
    Extract installed app list from the Android device via the companion APK.

    Parameters
    ----------
    serial:      ADB device serial string.
    staging_dir: Root staging directory.  APK files are written to
                 ``staging_dir/installed_apps/`` when ``include_apk=True``.
    is_rooted:   Unused (companion APK does not require root).
    include_apk: If True, also download APK files via binary streaming.

    Returns
    -------
    List of InstalledApp objects; empty list on failure.
    """
    try:
        from core.adb_manager import ADBManager
        adb = ADBManager(get_config())
        setup_adb_forward(adb, serial)
    except Exception as exc:
        logger.error("[installed_apps/android] ADB forward setup failed: %s", exc)
        return _fallback_adb(serial, staging_dir, is_rooted)

    try:
        with CompanionClient(timeout=120.0) as client:
            if not client.ping():
                logger.warning(
                    "[installed_apps/android] Companion APK not responding — "
                    "falling back to ADB path"
                )
                return _fallback_adb(serial, staging_dir, is_rooted)

            if include_apk:
                return _extract_with_apk(client, serial, staging_dir)
            else:
                return _extract_list_only(client, serial)

    except Exception:
        logger.exception("[installed_apps/android] Unhandled error during extraction")
        return _fallback_adb(serial, staging_dir, is_rooted)
    finally:
        try:
            teardown_adb_forward(adb, serial)
        except Exception:
            pass


def _extract_list_only(
    client: CompanionClient,
    serial: str,
) -> list[InstalledApp]:
    """Extract app metadata only (no APK files)."""
    response = client.send_recv({"cmd": "extract_installed_apps"})

    if response.get("status") != "ok":
        logger.warning(
            "[installed_apps/android] APK returned status '%s': %s",
            response.get("status"), response.get("message"),
        )
        return []

    raw_items: list[dict] = response.get("data", [])
    return _parse_app_list(raw_items, serial)


def _extract_with_apk(
    client: CompanionClient,
    serial: str,
    staging_dir: Path,
) -> list[InstalledApp]:
    """Extract app metadata and download APK files via binary streaming."""
    apk_dir = staging_dir / "installed_apps"
    apk_dir.mkdir(parents=True, exist_ok=True)

    app_list, apk_paths = client.extract_installed_apps_with_apk(apk_dir)

    # Build a mapping of package_name -> local APK path
    apk_map: dict[str, Path] = {}
    for p in apk_paths:
        # Filename is <package_name>.apk
        pkg = p.stem
        apk_map[pkg] = p

    apps = _parse_app_list(app_list, serial)

    # Attach local APK paths to the InstalledApp objects
    for app in apps:
        path = apk_map.get(app.package_name)
        if path and path.exists():
            app.apk_local_path = path

    apk_count = sum(1 for a in apps if a.apk_local_path is not None)
    logger.info(
        "[installed_apps/android] %d app(s) with APK files backed up from %s",
        apk_count, serial,
    )
    return apps


def _parse_app_list(
    raw_items: list[dict],
    serial: str,
) -> list[InstalledApp]:
    """Convert raw JSON dicts from the companion APK to InstalledApp objects."""
    if not raw_items:
        logger.info("[installed_apps/android] No installed apps found")
        return []

    apps: list[InstalledApp] = []
    for raw in raw_items:
        try:
            apps.append(InstalledApp(
                package_name=raw.get("package_name", ""),
                app_name=raw.get("app_name", ""),
                version_name=raw.get("version_name"),
                version_code=_safe_int(raw.get("version_code")),
                apk_size=_safe_int(raw.get("apk_size")) or 0,
                install_time=_safe_int(raw.get("install_time")),
                update_time=_safe_int(raw.get("update_time")),
                is_system=bool(raw.get("is_system", False)),
            ))
        except Exception as exc:
            logger.debug(
                "[installed_apps/android] Skipping malformed app entry: %s", exc
            )

    logger.info(
        "[installed_apps/android] Extracted %d installed app(s) from %s",
        len(apps), serial,
    )
    return apps


def _safe_int(val) -> int | None:
    """Safely convert a value to int, returning None on failure."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _fallback_adb(
    serial: str,
    staging_dir: Path,
    is_rooted: bool,
) -> list[InstalledApp]:
    """
    Fall back to the existing ADB-based app extraction.

    The ADB path returns list[dict] (AppInfo dicts), not InstalledApp objects.
    We convert them here for compatibility.
    """
    try:
        from core.extract_apps_android import extract as adb_extract
        logger.info(
            "[installed_apps/android] Falling back to ADB-based extraction"
        )
        adb_results = adb_extract(serial, staging_dir, is_rooted)
        # Convert ADB AppInfo dicts to InstalledApp objects
        apps: list[InstalledApp] = []
        for info in adb_results:
            apk_files = info.get("apk_files", [])
            first_apk = apk_files[0] if apk_files else None
            apps.append(InstalledApp(
                package_name=info.get("package", ""),
                version_name=info.get("version_name", ""),
                version_code=info.get("version_code"),
                apk_size=int(info.get("apk_size_mb", 0) * 1_048_576),
                apk_local_path=first_apk,
            ))
        return apps
    except ImportError:
        logger.debug(
            "[installed_apps/android] ADB-based extract_apps_android "
            "module not available"
        )
        return []
    except Exception as exc:
        logger.error(
            "[installed_apps/android] ADB fallback failed: %s", exc
        )
        return []
