"""
privilege_detector.py

Determines jailbreak status for iOS devices and root status for Android
devices.  Designed to be called after device_detector has identified the
connected hardware.

iOS detection probes (in descending reliability):
  1. Attempt to open the AFC2 service (com.apple.afc2) via pymobiledevice3.
     A successful open means AFC2 is installed — strongest jailbreak signal.
  2. Use standard AFC to stat known jailbreak directories (/var/jb, /private/var/jb, /bootstrap).
  3. Query the installation proxy for Cydia or Sileo bundle IDs.

Android detection probes:
  1. Run 'adb shell su -c id' — uid=0 confirms root shell.
  2. Stat known su binary paths (/system/xbin/su, /system/bin/su).
  3. Check Magisk artefacts (/data/adb/magisk, pm list packages | grep magisk).
  4. Check KernelSU artefacts (/data/adb/ksu).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

from core.config_loader import Config, get_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class JailbreakInfo:
    is_jailbroken: bool
    jailbreak_type: str | None          # "checkra1n" | "unc0ver" | "dopamine" | "palera1n" | "unknown"
    has_afc2: bool
    has_ldrestart: bool                 # /usr/bin/ldrestart present via AFC2
    substrate_type: str | None          # "substrate" | "substitute" | "ellekit" | None


@dataclass
class RootInfo:
    is_rooted: bool
    root_type: str | None               # "magisk" | "kernelsu" | "supersu" | "unknown"
    su_path: str | None


# ---------------------------------------------------------------------------
# iOS jailbreak detection
# ---------------------------------------------------------------------------

# Directories whose presence on standard AFC indicates a jailbreak
_JB_DIRS = [
    "/private/var/jb",
    "/var/jb",
    "/bootstrap",
    "/private/var/MobileDevice/PackageManifests",
]

# Cydia/package-manager bundle IDs
_JB_BUNDLE_IDS = {
    "com.saurik.Cydia": "cydia",
    "org.coolstar.SileoStore": "sileo",
    "xyz.willy.Zebra": "zebra",
}

# Jailbreak markers that hint at the specific jailbreak tool
_JB_MARKER_PATHS = {
    "checkra1n": ["/var/checkra1n.dmg", "/private/var/checkra1n.dmg"],
    "unc0ver": ["/var/jb/.installed_unc0ver"],
    "dopamine": ["/var/jb/.installed_dopamine"],
    "palera1n": ["/private/preboot/jb/procursus", "/var/jb/.installed_palera1n"],
}

_SUBSTRATE_PATHS = {
    "/Library/MobileSubstrate/MobileSubstrate.dylib": "substrate",
    "/usr/lib/TweakInject.dylib": "substitute",
    "/usr/lib/ellekit/ellekit.dylib": "ellekit",
}


def detect_ios_privileges(udid: str, config: Config | None = None) -> JailbreakInfo:
    """
    Return a JailbreakInfo for the given iOS device UDID.
    Never raises — all errors are logged and treated as 'not jailbroken'.

    The *config* parameter is accepted for API consistency with
    detect_android_privileges(); the iOS probe helpers call get_config()
    internally and do not currently accept a config argument.
    """

    has_afc2 = False
    has_ldrestart = False
    jb_type: str | None = None
    substrate: str | None = None
    jailbroken = False

    # ── Probe 1: AFC2 service ────────────────────────────────────────────────
    afc2_client = _try_open_afc2(udid)
    if afc2_client is not None:
        has_afc2 = True
        jailbroken = True
        logger.info("AFC2 service open on %s — device is jailbroken", udid)

        # While AFC2 is open, probe ldrestart and substrate
        has_ldrestart = _afc2_path_exists(afc2_client, "/usr/bin/ldrestart")
        for path, stype in _SUBSTRATE_PATHS.items():
            if _afc2_path_exists(afc2_client, path):
                substrate = stype
                break

        # Detect jailbreak type via marker files
        for jb_name, paths in _JB_MARKER_PATHS.items():
            for p in paths:
                if _afc2_path_exists(afc2_client, p):
                    jb_type = jb_name
                    break
            if jb_type:
                break

        try:
            afc2_client.close()  # type: ignore[attr-defined]
        except Exception:
            pass

    if not jailbroken:
        # ── Probe 2: Standard AFC — look for jailbreak directories ───────────
        afc_client = _try_open_afc(udid)
        if afc_client is not None:
            for jb_dir in _JB_DIRS:
                if _afc_path_exists(afc_client, jb_dir):
                    jailbroken = True
                    if jb_type is None:
                        jb_type = "unknown"
                    logger.info(
                        "Jailbreak dir found via AFC on %s: %s", udid, jb_dir
                    )
                    break
            try:
                afc_client.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    if not jailbroken:
        # ── Probe 3: Installation proxy — look for Cydia / Sileo ─────────────
        bundle_found = _check_jb_apps_via_instproxy(udid)
        if bundle_found:
            jailbroken = True
            if jb_type is None:
                jb_type = "unknown"
            logger.info(
                "Jailbreak app found via installation proxy on %s: %s",
                udid, bundle_found,
            )

    if jailbroken and jb_type is None:
        jb_type = "unknown"

    return JailbreakInfo(
        is_jailbroken=jailbroken,
        jailbreak_type=jb_type,
        has_afc2=has_afc2,
        has_ldrestart=has_ldrestart,
        substrate_type=substrate,
    )


def _try_open_afc2(udid: str) -> object | None:
    """
    Attempt to open an AFC2 session.  Returns the client on success, None
    otherwise.  Silences all exceptions.
    """
    try:
        from pymobiledevice3.lockdown import LockdownClient  # type: ignore[import]  # noqa: F401
        from pymobiledevice3.services.afc import AfcService  # type: ignore[import]

        lockdown = _make_lockdown(udid)
        if lockdown is None:
            return None

        # AFC2 is exposed as com.apple.afc2
        try:
            afc2 = AfcService(lockdown, service_name="com.apple.afc2")
            return afc2
        except Exception:
            return None
    except ImportError:
        return None
    except Exception as exc:
        logger.debug("AFC2 open attempt failed for %s: %s", udid, exc)
        return None


def _try_open_afc(udid: str) -> object | None:
    """Attempt to open a standard AFC session. Returns client or None."""
    try:
        from pymobiledevice3.lockdown import LockdownClient  # type: ignore[import]  # noqa: F401
        from pymobiledevice3.services.afc import AfcService  # type: ignore[import]

        lockdown = _make_lockdown(udid)
        if lockdown is None:
            return None

        afc = AfcService(lockdown)
        return afc
    except ImportError:
        return None
    except Exception as exc:
        logger.debug("AFC open attempt failed for %s: %s", udid, exc)
        return None


def _afc2_path_exists(client: object, path: str) -> bool:
    """Return True if *path* exists on the device via an AFC2 client."""
    return _afc_path_exists(client, path)


def _afc_path_exists(client: object, path: str) -> bool:
    """Return True if *path* exists on the device via an AFC client."""
    try:
        result = client.stat(path)  # type: ignore[attr-defined]
        return result is not None
    except Exception:
        return False


def _check_jb_apps_via_instproxy(udid: str) -> str | None:
    """
    Query the installation proxy for known jailbreak app bundle IDs.
    Returns the first matching bundle ID string, or None.
    """
    try:
        from pymobiledevice3.lockdown import LockdownClient  # type: ignore[import]  # noqa: F401
        from pymobiledevice3.services.installation_proxy import (  # type: ignore[import]
            InstallationProxyService,
        )

        lockdown = _make_lockdown(udid)
        if lockdown is None:
            return None

        instproxy = InstallationProxyService(lockdown)
        app_list = instproxy.get_apps()  # returns dict bundle_id -> info
        for bundle_id in _JB_BUNDLE_IDS:
            if bundle_id in app_list:
                return bundle_id
        return None
    except ImportError:
        return None
    except Exception as exc:
        logger.debug("installation_proxy check failed for %s: %s", udid, exc)
        return None


def _make_lockdown(udid: str) -> object | None:
    """Create a LockdownClient for *udid*, returning None on any error."""
    try:
        from pymobiledevice3.lockdown import LockdownClient  # type: ignore[import]  # noqa: F401
        try:
            return LockdownClient(serial=udid)
        except TypeError:
            return LockdownClient(udid)  # type: ignore[call-arg]
    except Exception as exc:
        logger.debug("LockdownClient creation failed for %s: %s", udid, exc)
        return None


# ---------------------------------------------------------------------------
# Android root detection
# ---------------------------------------------------------------------------

_SU_PATHS = [
    "/system/xbin/su",
    "/system/bin/su",
    "/sbin/su",
    "/su/bin/su",
]


def detect_android_privileges(serial: str, config: Config | None = None) -> RootInfo:
    """
    Return a RootInfo for the given ADB serial.
    Never raises — all errors are logged and treated as 'not rooted'.
    """
    cfg = config or get_config()

    # ── Probe 1: su -c id ────────────────────────────────────────────────────
    uid_output = _adb_shell(serial, "su -c id", cfg, timeout=8)
    if uid_output and "uid=0" in uid_output:
        root_type, su_path = _identify_root_type(serial, cfg)
        logger.info("Root confirmed via su on %s (type=%s)", serial, root_type)
        return RootInfo(is_rooted=True, root_type=root_type, su_path=su_path)

    # ── Probe 2: ls known su paths ───────────────────────────────────────────
    for su_path in _SU_PATHS:
        ls_out = _adb_shell(serial, f"ls {su_path}", cfg, timeout=6)
        if ls_out and su_path in ls_out and "No such" not in ls_out:
            root_type, _ = _identify_root_type(serial, cfg)
            logger.info("su binary found at %s on %s", su_path, serial)
            return RootInfo(is_rooted=True, root_type=root_type, su_path=su_path)

    # ── Probe 3: Magisk ──────────────────────────────────────────────────────
    magisk_dir = _adb_shell(serial, "ls /data/adb/magisk", cfg, timeout=6)
    if magisk_dir and "No such" not in magisk_dir and magisk_dir.strip():
        logger.info("Magisk directory found on %s", serial)
        return RootInfo(is_rooted=True, root_type="magisk", su_path=None)

    magisk_pkg = _adb_shell(serial, "pm list packages", cfg, timeout=15)
    if magisk_pkg and "magisk" in magisk_pkg.lower():
        logger.info("Magisk package found on %s", serial)
        return RootInfo(is_rooted=True, root_type="magisk", su_path=None)

    # ── Probe 4: KernelSU ────────────────────────────────────────────────────
    ksu_dir = _adb_shell(serial, "ls /data/adb/ksu", cfg, timeout=6)
    if ksu_dir and "No such" not in ksu_dir and ksu_dir.strip():
        logger.info("KernelSU directory found on %s", serial)
        return RootInfo(is_rooted=True, root_type="kernelsu", su_path=None)

    logger.info("No root indicators found on Android device %s", serial)
    return RootInfo(is_rooted=False, root_type=None, su_path=None)


def _identify_root_type(serial: str, cfg: Config) -> tuple[str, str | None]:
    """
    Given that root access is confirmed, identify the root solution and
    return (root_type, su_path).
    """
    # Check Magisk first (most common)
    magisk = _adb_shell(serial, "ls /data/adb/magisk", cfg, timeout=6)
    if magisk and "No such" not in magisk and magisk.strip():
        return "magisk", "/data/adb/magisk/magisk"

    # KernelSU
    ksu = _adb_shell(serial, "ls /data/adb/ksu", cfg, timeout=6)
    if ksu and "No such" not in ksu and ksu.strip():
        return "kernelsu", None

    # SuperSU
    supersu = _adb_shell(serial, "ls /system/xbin/daemonsu", cfg, timeout=6)
    if supersu and "No such" not in supersu and supersu.strip():
        return "supersu", "/system/xbin/su"

    # Locate su binary for generic "unknown" root
    for path in _SU_PATHS:
        ls = _adb_shell(serial, f"ls {path}", cfg, timeout=6)
        if ls and "No such" not in ls and ls.strip():
            return "unknown", path

    return "unknown", None


def _adb_shell(serial: str, cmd: str, cfg: Config, timeout: int = 10) -> str | None:
    """
    Run 'adb -s <serial> shell <cmd>' and return stdout string, or None on
    failure.  stderr is silently discarded.
    """
    try:
        result = subprocess.run(
            [str(cfg.adb_exe), "-s", serial, "shell", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout
    except subprocess.TimeoutExpired:
        logger.debug("adb shell timeout: %s on %s", cmd, serial)
        return None
    except FileNotFoundError:
        logger.error("adb.exe not found: %s", cfg.adb_exe)
        return None
    except Exception as exc:
        logger.debug("adb shell error (%s): %s", cmd, exc)
        return None
