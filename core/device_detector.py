"""
device_detector.py

Detects all iOS and Android devices currently connected via USB to the host PC.

Detection strategy
------------------
iOS (primary):   pymobiledevice3  — pure-Python, no subprocess overhead.
iOS (fallback):  bin/libimobiledevice/idevice_id.exe + ideviceinfo.exe
                 Used when pymobiledevice3 is unavailable or fails.

Android:         bin/adb/adb.exe  — 'adb devices -l' plus per-device getprop
                 calls to retrieve model name and OS version.

is_jailbroken / is_rooted are left False here; use privilege_detector to
fill those fields after initial device discovery.
"""

from __future__ import annotations

import inspect
import logging
import subprocess
from typing import Any

from core.config_loader import Config, get_config
from core.normalization_schema import DeviceInfo
from core.pmd3_asyncio import pmd3_run

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_all_devices(config: Config | None = None) -> list[DeviceInfo]:
    """
    Return a combined list of all connected iOS and Android devices.
    Never raises — logs errors and returns partial results.
    """
    cfg = config or get_config()
    devices: list[DeviceInfo] = []
    devices.extend(detect_ios_devices(cfg))
    devices.extend(detect_android_devices(cfg))
    logger.info(
        "Device scan complete: %d iOS, %d Android (%d total)",
        sum(1 for d in devices if d.platform == "ios"),
        sum(1 for d in devices if d.platform == "android"),
        len(devices),
    )
    return devices


def detect_ios_devices(config: Config | None = None) -> list[DeviceInfo]:
    """
    Return DeviceInfo for every connected iOS device.
    Tries pymobiledevice3 first, falls back to idevice_id.exe.
    """
    cfg = config or get_config()

    # ── Primary: pymobiledevice3 ─────────────────────────────────────────────
    try:
        devices = _ios_via_pymobiledevice3(cfg)
        if devices is not None:
            return devices
    except Exception as exc:
        logger.warning("pymobiledevice3 detection failed (%s); trying fallback", exc)

    # ── Fallback: libimobiledevice EXEs ──────────────────────────────────────
    try:
        return _ios_via_libimobiledevice(cfg)
    except Exception as exc:
        logger.error("iOS fallback detection failed: %s", exc)
        return []


def detect_android_devices(config: Config | None = None) -> list[DeviceInfo]:
    """Return DeviceInfo for every connected Android device visible to ADB."""
    cfg = config or get_config()
    try:
        return _android_via_adb(cfg)
    except Exception as exc:
        logger.error("Android detection failed: %s", exc)
        return []


def get_ios_device_info(udid: str, config: Config | None = None) -> DeviceInfo:
    """
    Query full DeviceInfo for a specific iOS UDID.
    Raises RuntimeError if the device cannot be queried.
    """
    cfg = config or get_config()

    # Try pymobiledevice3 first
    try:
        info = _ios_info_via_pymobiledevice3(udid)
        if info is not None:
            return info
    except Exception as exc:
        logger.warning("pymobiledevice3 info query failed for %s: %s", udid, exc)

    # Fallback: ideviceinfo.exe
    return _ios_info_via_ideviceinfo(udid, cfg)


def get_android_device_info(serial: str, config: Config | None = None) -> DeviceInfo:
    """
    Query full DeviceInfo for a specific ADB serial.
    Raises RuntimeError if the device cannot be queried.
    """
    cfg = config or get_config()
    return _android_info_via_adb(serial, cfg)


# ---------------------------------------------------------------------------
# iOS via pymobiledevice3
# ---------------------------------------------------------------------------

def _ios_via_pymobiledevice3(cfg: Config) -> list[DeviceInfo] | None:
    """
    Use pymobiledevice3 to enumerate connected iOS devices.
    Returns None if the library is not importable (triggers fallback).
    """
    try:
        # Import guard — return None instead of crashing if not installed
        from pymobiledevice3.usbmux import select_devices_by_connection_type  # type: ignore[import]
    except ImportError:
        logger.debug("pymobiledevice3 not available — will use libimobiledevice fallback")
        return None

    try:
        result = select_devices_by_connection_type(connection_type="USB")
        # pymobiledevice3 9.x made this function async
        if inspect.iscoroutine(result):
            result = pmd3_run(result)
        connected = result
    except Exception as exc:
        logger.warning("select_devices_by_connection_type failed: %s", exc)
        return None

    devices: list[DeviceInfo] = []
    for dev in connected:
        try:
            udid = dev.serial if hasattr(dev, "serial") else str(dev)
            info = _ios_info_via_pymobiledevice3(udid)
            if info:
                devices.append(info)
        except Exception as exc:
            logger.warning("Failed to query iOS device %s: %s", dev, exc)

    return devices


def _create_lockdown_for_udid(udid: str) -> Any | None:
    """
    Create a pymobiledevice3 lockdown client for *udid*.

    Tries in order:
      1. create_using_usbmux(serial=udid)  — pmd3 9.x factory (LockdownClient is abstract)
      2. LockdownClient(serial=udid)        — pmd3 4–8.x
      3. LockdownClient(udid)              — pmd3 <4.x (positional)
    Returns None (instead of raising) so callers can gracefully fall back.
    """
    # Attempt 1: modern factory (pmd3 9.x)
    # Note: create_using_usbmux is itself async in pmd3 9.x.
    try:
        from pymobiledevice3.lockdown import create_using_usbmux  # type: ignore[import]
        result = create_using_usbmux(serial=udid)
        if inspect.iscoroutine(result):
            result = pmd3_run(result)
        return result
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("create_using_usbmux failed for %s: %s", udid, exc)

    # Attempt 2: keyword-arg constructor (pmd3 4–8.x)
    try:
        from pymobiledevice3.lockdown import LockdownClient  # type: ignore[import]
        result = LockdownClient(serial=udid)
        if inspect.iscoroutine(result):
            result = pmd3_run(result)
        return result
    except ImportError:
        return None
    except TypeError:
        pass  # no 'serial' kwarg
    except Exception as exc:
        logger.debug("LockdownClient(serial=) failed for %s: %s", udid, exc)

    # Attempt 3: positional constructor (pmd3 <4.x)
    try:
        from pymobiledevice3.lockdown import LockdownClient  # type: ignore[import]
        result = LockdownClient(udid)  # type: ignore[call-arg]
        if inspect.iscoroutine(result):
            result = pmd3_run(result)
        return result
    except Exception as exc:
        logger.warning("LockdownClient creation failed for %s: %s", udid, exc)
        return None


def _ios_info_via_pymobiledevice3(udid: str) -> DeviceInfo | None:
    """Query DeviceInfo for one UDID via pymobiledevice3 lockdown."""
    lockdown = _create_lockdown_for_udid(udid)
    if lockdown is None:
        return None

    try:
        model = _lockdown_get(lockdown, "ProductType") or "Unknown"
        name = _lockdown_get(lockdown, "DeviceName") or "iPhone"
        version = _lockdown_get(lockdown, "ProductVersion") or "Unknown"
        real_udid = _lockdown_get(lockdown, "UniqueDeviceID") or udid

        return DeviceInfo(
            udid=real_udid,
            platform="ios",
            model=model,
            name=name,
            os_version=version,
            is_jailbroken=False,
            is_rooted=False,
            serial=real_udid,
        )
    except Exception as exc:
        logger.warning("Lockdown query error for %s: %s", udid, exc)
        return None
    finally:
        try:
            result = lockdown.close()  # type: ignore[attr-defined]
            if inspect.iscoroutine(result):
                pmd3_run(result)
        except Exception:
            pass


def _lockdown_get(lockdown: Any, key: str) -> str | None:
    """Safe wrapper around lockdown key queries (API varies by pmd3 version)."""
    try:
        if hasattr(lockdown, "get_value"):
            result = lockdown.get_value(key)
            if inspect.iscoroutine(result):
                result = pmd3_run(result)
            return result
        if hasattr(lockdown, "get"):
            result = lockdown.get(key)
            if inspect.iscoroutine(result):
                result = pmd3_run(result)
            return result
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# iOS via libimobiledevice EXEs
# ---------------------------------------------------------------------------

def _ios_via_libimobiledevice(cfg: Config) -> list[DeviceInfo]:
    """Use idevice_id.exe to list UDIDs, then query each with ideviceinfo.exe."""
    idevice_id = cfg.idevice_bins.get("idevice_id")
    if not idevice_id:
        logger.error("idevice_id not in idevice_bins — cannot list iOS devices")
        return []

    env = _limd_env(cfg)

    try:
        result = subprocess.run(
            [str(idevice_id), "-l"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except subprocess.TimeoutExpired:
        logger.error("idevice_id -l timed out")
        return []
    except FileNotFoundError:
        logger.error("idevice_id.exe not executable: %s", idevice_id)
        return []

    udids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    logger.debug("idevice_id found %d UDIDs: %s", len(udids), udids)

    devices: list[DeviceInfo] = []
    for udid in udids:
        try:
            info = _ios_info_via_ideviceinfo(udid, cfg)
            devices.append(info)
        except Exception as exc:
            logger.warning("Could not query iOS device %s: %s", udid, exc)

    return devices


def _ios_info_via_ideviceinfo(udid: str, cfg: Config) -> DeviceInfo:
    """
    Call ideviceinfo.exe -u <udid> and parse key: value lines to build
    a DeviceInfo.  Raises RuntimeError on failure.
    """
    ideviceinfo = cfg.idevice_bins.get("ideviceinfo")
    if not ideviceinfo:
        raise RuntimeError("ideviceinfo not found in config.idevice_bins")

    env = _limd_env(cfg)

    try:
        result = subprocess.run(
            [str(ideviceinfo), "-u", udid],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ideviceinfo timed out for UDID {udid}")
    except FileNotFoundError as exc:
        raise RuntimeError(f"ideviceinfo.exe not executable: {exc}") from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"ideviceinfo failed (rc={result.returncode}) for UDID {udid}: "
            f"{result.stderr.strip()}"
        )

    props = _parse_key_value(result.stdout)
    model = props.get("ProductType", "Unknown")
    name = props.get("DeviceName", "iPhone")
    version = props.get("ProductVersion", "Unknown")

    return DeviceInfo(
        udid=udid,
        platform="ios",
        model=model,
        name=name,
        os_version=version,
        is_jailbroken=False,
        is_rooted=False,
        serial=udid,
    )


# ---------------------------------------------------------------------------
# Android via ADB
# ---------------------------------------------------------------------------

def _android_via_adb(cfg: Config) -> list[DeviceInfo]:
    """Parse 'adb devices -l' to find attached Android devices."""
    try:
        result = subprocess.run(
            [str(cfg.adb_exe), "devices", "-l"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        logger.error("adb devices -l timed out")
        return []
    except FileNotFoundError:
        logger.error("adb.exe not found at: %s", cfg.adb_exe)
        return []

    serials = _parse_adb_devices(result.stdout)
    logger.debug("adb found %d Android device(s): %s", len(serials), serials)

    devices: list[DeviceInfo] = []
    for serial in serials:
        try:
            info = _android_info_via_adb(serial, cfg)
            devices.append(info)
        except Exception as exc:
            logger.warning("Could not query Android device %s: %s", serial, exc)

    return devices


def _android_info_via_adb(serial: str, cfg: Config) -> DeviceInfo:
    """
    Query model and OS version for a single Android device via adb getprop.
    Raises RuntimeError on failure.
    """
    model = _adb_getprop(serial, "ro.product.model", cfg) or "Unknown Android"
    version = _adb_getprop(serial, "ro.build.version.release", cfg) or "Unknown"
    brand = _adb_getprop(serial, "ro.product.brand", cfg) or ""
    # ro.product.name is a build codename (e.g. "a14xm"), not a marketing name.
    # Use the model string directly — resolve_android_name() in device_names.py
    # will prepend the brand when constructing the display label.
    name = model

    return DeviceInfo(
        udid=serial,
        platform="android",
        model=model,
        name=name,
        os_version=version,
        is_jailbroken=False,
        is_rooted=False,
        serial=serial,
        brand=brand,
    )


def _adb_getprop(serial: str, prop: str, cfg: Config) -> str | None:
    """Run 'adb -s <serial> shell getprop <prop>' and return stripped stdout."""
    try:
        result = subprocess.run(
            [str(cfg.adb_exe), "-s", serial, "shell", "getprop", prop],
            capture_output=True,
            text=True,
            timeout=10,
        )
        value = result.stdout.strip()
        return value if value else None
    except Exception as exc:
        logger.debug("getprop %s failed for %s: %s", prop, serial, exc)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_adb_devices(output: str) -> list[str]:
    """
    Parse 'adb devices -l' output and return serials of online devices.
    Skips offline, unauthorized, and non-device lines, but logs actionable
    warnings so the user knows why a device isn't appearing.
    """
    serials: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, status = parts[0], parts[1]
        if status == "device":
            serials.append(serial)
        elif status == "unauthorized":
            logger.warning(
                "Android device %s is connected but not authorized. "
                "On the phone, tap 'Allow' on the 'Allow USB debugging?' dialog.",
                serial,
            )
        elif status == "offline":
            logger.warning(
                "Android device %s is offline. "
                "Try unplugging and reconnecting the USB cable.",
                serial,
            )
        else:
            logger.warning(
                "Android device %s has unexpected ADB status '%s' — skipping.",
                serial, status,
            )
    return serials


def _parse_key_value(text: str) -> dict[str, str]:
    """Parse 'Key: Value' lines into a dict (used for ideviceinfo output)."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


def _limd_env(cfg: Config) -> dict[str, str] | None:
    """
    Build a subprocess environment dict that adds the libimobiledevice
    directory to PATH so Windows can resolve the required DLLs.
    """
    import os
    env = os.environ.copy()
    limd_str = str(cfg.libimobiledevice_dir)
    if limd_str not in env.get("PATH", ""):
        env["PATH"] = limd_str + ";" + env.get("PATH", "")
    return env
