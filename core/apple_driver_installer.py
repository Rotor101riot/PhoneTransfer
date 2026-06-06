"""
apple_driver_installer.py

Manages the bundled Apple USB drivers (USBAAPL64 and AppleUSB) that Windows
needs to communicate with iOS devices over USB without iTunes installed.

Driver files are stored in:
    bin/drivers/apple_usb/usbaapl64/   — Apple Mobile Device USB Driver
    bin/drivers/apple_usb/appleusb/    — Apple USB (WinUSB-based, newer devices)

Usage
-----
    from core.apple_driver_installer import check_drivers_installed, install_drivers

    if not check_drivers_installed():
        install_drivers()   # prompts for UAC elevation

The install function launches an elevated PowerShell process via ShellExecuteW
("runas") so the main process never needs to run as admin itself.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _drivers_root() -> Path:
    """Return bin/drivers/apple_usb/ relative to this file's project root."""
    return Path(__file__).parent.parent / "bin" / "drivers" / "apple_usb"


def usbaapl64_inf() -> Path:
    return _drivers_root() / "usbaapl64" / "usbaapl64.inf"


def appleusb_inf() -> Path:
    return _drivers_root() / "appleusb" / "AppleUsb.inf"


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------

def check_drivers_installed() -> bool:
    """
    Return True if at least one of the Apple USB drivers appears to be
    installed on this system.

    Uses 'pnputil /enum-drivers' and looks for the INF original names.
    Falls back to a quick registry probe on failure.
    """
    try:
        result = subprocess.run(
            ["pnputil", "/enum-drivers"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        output_lower = result.stdout.lower()
        if "usbaapl64.inf" in output_lower or "appleusb.inf" in output_lower:
            logger.debug("Apple USB driver found via pnputil")
            return True
    except Exception as exc:
        logger.debug("pnputil check failed: %s", exc)

    # Fallback: check for the USBAAPL64 service key in the registry
    try:
        import winreg
        key_path = r"SYSTEM\CurrentControlSet\Services\USBAAPL64"
        winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path).Close()
        logger.debug("Apple USB driver found via registry")
        return True
    except (OSError, AttributeError):
        pass

    return False


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def install_drivers() -> bool:
    """
    Install both Apple USB drivers using pnputil, elevated via UAC.

    Launches an elevated PowerShell window that calls:
        pnputil /add-driver <inf> /install

    Returns True if the elevated process launched successfully (not necessarily
    that installation succeeded — check check_drivers_installed() afterwards).
    """
    usbaapl = usbaapl64_inf()
    appleusb = appleusb_inf()

    if not usbaapl.exists():
        logger.error("usbaapl64.inf not found at: %s", usbaapl)
        return False
    if not appleusb.exists():
        logger.error("AppleUsb.inf not found at: %s", appleusb)
        return False

    # Build a PowerShell script that installs both drivers and shows a result.
    ps_script = (
        f"pnputil /add-driver '{usbaapl}' /install; "
        f"pnputil /add-driver '{appleusb}' /install; "
        f"Read-Host 'Done. Press Enter to close'"
    )

    try:
        # Start-Process with -Verb RunAs triggers UAC elevation.
        subprocess.run(
            [
                "powershell.exe",
                "-NonInteractive",
                "-Command",
                f"Start-Process powershell.exe "
                f"-ArgumentList '-Command \"{ps_script}\"' "
                f"-Verb RunAs -Wait",
            ],
            timeout=120,
        )
        logger.info("Apple USB driver installation process completed")
        return True
    except subprocess.TimeoutExpired:
        logger.warning("Driver installation timed out")
        return False
    except Exception as exc:
        logger.error("Driver installation failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def ensure_drivers(prompt_callback=None) -> bool:
    """
    Check if drivers are installed; install them if not.

    prompt_callback: optional callable(message: str) -> bool that asks the
    user for confirmation before installing.  If None, installs immediately.

    Returns True if drivers are (now) installed.
    """
    if check_drivers_installed():
        return True

    if prompt_callback is not None:
        confirmed = prompt_callback(
            "Apple USB drivers are not installed.\n"
            "PhoneTransfer needs them to communicate with iOS devices.\n\n"
            "Install now? (requires administrator permission)"
        )
        if not confirmed:
            return False

    install_drivers()
    return check_drivers_installed()
