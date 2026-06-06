"""
prerequisite_checker.py

Checks, upgrades, and installs all PhoneTransfer prerequisites before the
main application window is shown.

Handled automatically (no user interaction required):
  - VC++ Redistributable 2015-2022  (registry check → bundled silent install)
  - Python packages                  (importlib.metadata → pip upgrade if outdated)

Handled with a prompt (cannot be automated):
  - Apple Devices / iTunes           (Windows service check → show install link)

Detected and reported (driver install offered separately):
  - Android USB recognition          (adb devices → warn if device unrecognised)

Usage
-----
    from core.prerequisite_checker import PrereqChecker

    checker = PrereqChecker()
    report  = checker.check_all()          # fast, read-only pass
    checker.fix_all(report, prompt_fn)     # installs / upgrades what it can
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Android USB driver INFs to look for in the Windows driver store.
# Checked via `pnputil /enum-drivers` — no signature verification, presence only.
#
# android_winusb.inf (Google USB Driver) is the only required entry: it covers
# ADB mode for virtually every Android OEM.  The rest are OEM-specific extras
# reported in the detail string so the user knows what coverage they have.
# ---------------------------------------------------------------------------

_ANDROID_DRIVER_INFS: dict[str, str] = {
    "android_winusb.inf": "Google USB Driver (ADB universal)",
    "ssudbus.inf":        "Samsung (ssudbus)",
    "ssadbus.inf":        "Samsung ADB (ssadbus)",
    "cdc-acm.inf":        "MediaTek COM port",
    "motoandroid.inf":    "Motorola",
    "motoandroid2.inf":   "Motorola (v2)",
    "lgandnetadb.inf":    "LG ADB",
    "diagswitchdrv.inf":  "Huawei",
}

# The minimum driver that must be present for Android ADB to work on most devices.
_ANDROID_DRIVER_REQUIRED = "android_winusb.inf"


# ---------------------------------------------------------------------------
# Minimum required Python package versions
# Mirrors requirements.txt — keep in sync when bumping versions.
# Keys are the importlib.metadata distribution names (case-insensitive).
# ---------------------------------------------------------------------------

_REQUIRED_PACKAGES: dict[str, str] = {
    "customtkinter":         "5.2.2",
    "pymobiledevice3":       "4.14.16",
    "iOSbackup":             "0.9.925",
    "iphone_backup_decrypt": "0.9.0",
    "pycryptodome":          "3.20.0",
    "cryptography":          "41.0.0",
    "wa-crypt-tools":        "0.1.0",
    "sqlcipher3":            "0.6.0",
    "vobject":               "0.9.9",
    "Pillow":                "10.3.0",
    "pillow-heif":           "0.16.0",
}

# pip install name for packages whose distribution name differs from
# the human-readable name used as a key in _REQUIRED_PACKAGES.
_PIP_NAME: dict[str, str] = {
    "iOSbackup":             "iOSbackup",
    "iphone_backup_decrypt": "iphone_backup_decrypt",
    "wa-crypt-tools":        "wa-crypt-tools",
    "pillow-heif":           "pillow-heif",
    "pycryptodome":          "pycryptodome",
    "sqlcipher3":            "sqlcipher3",
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class PackageStatus:
    name: str
    required: str          # minimum version string
    installed: str | None  # None = not installed
    action: str            # "ok" | "upgrade" | "install" | "skipped"
    error: str | None = None


@dataclass
class SystemStatus:
    name: str
    present: bool
    action: str            # "ok" | "installed" | "prompt" | "failed"
    detail: str = ""


@dataclass
class PrereqReport:
    packages: list[PackageStatus] = field(default_factory=list)
    system:   list[SystemStatus]  = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        pkg_ok = all(s.action in ("ok", "skipped") for s in self.packages)
        # Exclude "Android ADB" — it reflects runtime device state, not an
        # installation prerequisite.  An unauthorized/offline device at startup
        # should not trigger the "prerequisites need attention" path.
        sys_ok = all(
            s.action in ("ok", "installed")
            for s in self.system
            if s.name != "Android ADB"
        )
        return pkg_ok and sys_ok

    @property
    def needs_attention(self) -> list[str]:
        """Human-readable list of items that still need user action."""
        items: list[str] = []
        for s in self.packages:
            if s.action not in ("ok", "skipped"):
                items.append(f"Python package '{s.name}' — {s.action}")
        for s in self.system:
            if s.action == "prompt":
                items.append(f"{s.name} — {s.detail}")
            elif s.action == "failed":
                items.append(f"{s.name} (install failed) — {s.detail}")
        return items


# ---------------------------------------------------------------------------
# Main checker class
# ---------------------------------------------------------------------------

class PrereqChecker:
    """
    Checks and optionally fixes all prerequisites.

    Parameters
    ----------
    project_root:
        Override the auto-detected project root (useful in tests).
    """

    def __init__(self, project_root: Path | None = None) -> None:
        if project_root is None:
            project_root = Path(__file__).resolve().parent.parent
        self.project_root = project_root
        self._redist_dir = project_root / "bin" / "redist"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_all(self) -> PrereqReport:
        """
        Fast, read-only scan of all prerequisites.
        Does NOT install or modify anything.

        Returns a PrereqReport describing what is ok, outdated, missing,
        or needs manual action.
        """
        report = PrereqReport()
        report.packages = self._check_packages()
        report.system   = self._check_system()
        return report

    def fix_all(
        self,
        report: PrereqReport,
        prompt_fn: Callable[[str], bool] | None = None,
    ) -> PrereqReport:
        """
        Act on a previously-obtained PrereqReport.

        - Python packages:    auto-upgrades / installs via pip (no prompt).
        - VC++ Redistributable: silent install from bin/redist/ (UAC prompt
          from Windows, not from this app).
        - Apple Devices:      calls prompt_fn with a message if not present;
          cannot auto-install Apple software.
        - Android drivers:    not acted on here — caller handles via UI.

        Parameters
        ----------
        report:
            Result of a prior check_all() call.
        prompt_fn:
            Optional callable(message: str) -> bool.  Called when the user
            must take manual action (e.g. install Apple Devices).  If None,
            the message is logged as a warning instead.

        Returns
        -------
        A fresh PrereqReport reflecting the state after fixes were applied.
        """
        self._fix_packages(report.packages)
        self._fix_system(report.system, prompt_fn)
        # Re-check so callers always get an accurate final state.
        return self.check_all()

    # ------------------------------------------------------------------
    # Python package checks
    # ------------------------------------------------------------------

    def _check_packages(self) -> list[PackageStatus]:
        from importlib.metadata import version as pkg_version, PackageNotFoundError

        statuses: list[PackageStatus] = []
        for dist_name, min_ver in _REQUIRED_PACKAGES.items():
            try:
                installed = pkg_version(dist_name)
                if _version_gte(installed, min_ver):
                    action = "ok"
                else:
                    action = "upgrade"
            except PackageNotFoundError:
                installed = None
                action = "install"
            except Exception as exc:
                installed = None
                action = "skipped"
                logger.debug("Version check failed for %s: %s", dist_name, exc)

            statuses.append(PackageStatus(
                name=dist_name,
                required=min_ver,
                installed=installed,
                action=action,
            ))
            if action != "ok":
                logger.debug(
                    "Package '%s': installed=%s required>=%s → %s",
                    dist_name, installed, min_ver, action,
                )

        return statuses

    def _fix_packages(self, statuses: list[PackageStatus]) -> None:
        to_install   = [s for s in statuses if s.action == "install"]
        to_upgrade   = [s for s in statuses if s.action == "upgrade"]

        if to_install:
            logger.info(
                "Installing missing packages: %s",
                [s.name for s in to_install],
            )
            self._pip_install([_pip_name(s.name) for s in to_install])

        if to_upgrade:
            logger.info(
                "Upgrading outdated packages: %s",
                [f"{s.name} {s.installed}->{s.required}" for s in to_upgrade],
            )
            self._pip_install(
                [f"{_pip_name(s.name)}>={s.required}" for s in to_upgrade],
                upgrade=True,
            )

    @staticmethod
    def _pip_install(specs: list[str], upgrade: bool = False) -> None:
        """Run pip install for the given package specs."""
        cmd = [sys.executable, "-m", "pip", "install", "--quiet"]
        if upgrade:
            cmd.append("--upgrade")
        cmd.extend(specs)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                logger.warning(
                    "pip install returned rc=%d: %s", result.returncode, result.stderr.strip()
                )
            else:
                logger.info("pip install succeeded: %s", specs)
        except subprocess.TimeoutExpired:
            logger.warning("pip install timed out for: %s", specs)
        except Exception as exc:
            logger.error("pip install failed: %s", exc)

    # ------------------------------------------------------------------
    # System-level checks
    # ------------------------------------------------------------------

    def _check_system(self) -> list[SystemStatus]:
        return [
            self._check_vcredist(),
            self._check_apple_service(),
            self._check_android_drivers(),
            self._check_bundled_drivers(),
            self._check_adb_device(),
        ]

    def _fix_system(
        self,
        statuses: list[SystemStatus],
        prompt_fn: Callable[[str], bool] | None,
    ) -> None:
        for s in statuses:
            if s.name == "VC++ Redistributable" and not s.present:
                self._install_vcredist(s)
            elif s.name == "Apple Devices / iTunes" and not s.present:
                self._install_itunes(s, prompt_fn)
            elif s.name == "Bundled Android Drivers" and not s.present:
                self._install_android_drivers(s)

    # ── VC++ Redistributable ───────────────────────────────────────────

    def _check_vcredist(self) -> SystemStatus:
        """Return present=True if VC++ 2015-2022 x64 is installed."""
        present = _vcredist_installed()
        return SystemStatus(
            name="VC++ Redistributable",
            present=present,
            action="ok" if present else "install",
            detail="" if present else "Required by ADB. Will be installed from bin/redist/.",
        )

    def _install_vcredist(self, status: SystemStatus) -> None:
        exe = self._redist_dir / "VC_redist.x64.exe"
        if not exe.exists():
            logger.error("[prereq] VC_redist.x64.exe not found at: %s", exe)
            status.action = "failed"
            status.detail = f"Installer not found at {exe}"
            return

        logger.info("[prereq] Installing VC++ Redistributable silently from %s", exe)
        try:
            result = subprocess.run(
                [str(exe), "/install", "/quiet", "/norestart"],
                timeout=120,
            )
            if result.returncode in (0, 3010):
                # 0 = success, 3010 = success + reboot pending (acceptable)
                status.present = True
                status.action = "installed"
                logger.info("[prereq] VC++ Redistributable installed (rc=%d)", result.returncode)
            else:
                status.action = "failed"
                status.detail = f"Installer returned rc={result.returncode}"
                logger.warning("[prereq] VC++ install returned rc=%d", result.returncode)
        except subprocess.TimeoutExpired:
            status.action = "failed"
            status.detail = "Installer timed out"
            logger.warning("[prereq] VC++ install timed out")
        except Exception as exc:
            status.action = "failed"
            status.detail = str(exc)
            logger.error("[prereq] VC++ install error: %s", exc)

    # ── Apple Devices / iTunes ─────────────────────────────────────────

    def _check_apple_service(self) -> SystemStatus:
        """Return present=True if AppleMobileDeviceService is registered."""
        present = _apple_service_present()
        return SystemStatus(
            name="Apple Devices / iTunes",
            present=present,
            action="ok" if present else "install",
            detail=(
                ""
                if present
                else "iTunes not detected. Will be installed from bin/redist/iTunes64Setup.exe."
            ),
        )

    def _install_itunes(
        self,
        status: SystemStatus,
        prompt_fn: Callable[[str], bool] | None,
    ) -> None:
        exe = self._redist_dir / "iTunes64Setup.exe"
        if not exe.exists():
            logger.error("[prereq] iTunes64Setup.exe not found at: %s", exe)
            status.action = "failed"
            status.detail = f"Installer not found at {exe}"
            return

        # Ask the user before launching the iTunes installer — it shows its own UI.
        msg = (
            "iTunes is required for iOS transfers and is not installed.\n\n"
            "PhoneTransfer will now launch the iTunes installer.\n"
            "Complete the installation, then PhoneTransfer will continue."
        )
        if prompt_fn is not None:
            confirmed = prompt_fn(msg)
            if not confirmed:
                status.action = "prompt"
                status.detail = "iTunes install deferred by user. iOS transfers will not work."
                logger.info("[prereq] iTunes install deferred by user.")
                return
        else:
            logger.warning("[prereq] iTunes not installed. Launching installer: %s", exe)

        logger.info("[prereq] Launching iTunes installer: %s", exe)
        try:
            # iTunes setup has its own wizard UI — run it and wait for completion.
            result = subprocess.run([str(exe)], timeout=600)
            if _apple_service_present():
                status.present = True
                status.action = "installed"
                logger.info("[prereq] iTunes installed successfully.")
            else:
                status.action = "failed"
                status.detail = (
                    "iTunes installer finished but AppleMobileDeviceService was not found. "
                    "Try restarting PhoneTransfer after rebooting."
                )
                logger.warning("[prereq] iTunes installer ran but service not detected (rc=%d).", result.returncode)
        except subprocess.TimeoutExpired:
            status.action = "failed"
            status.detail = "iTunes installer timed out (10 min). Complete it manually and relaunch."
            logger.warning("[prereq] iTunes installer timed out.")
        except Exception as exc:
            status.action = "failed"
            status.detail = str(exc)
            logger.error("[prereq] iTunes install error: %s", exc)

    # ── Bundled Android OEM driver installers (Chimera bundle) ──────────

    _DRIVER_MARKER = ".drivers_installed"

    def _check_bundled_drivers(self) -> SystemStatus:
        """
        Check whether the bundled Chimera OEM driver installers have already
        been run.  Uses a marker file in the driver directory — the pnputil
        driver-store check is unreliable (stale entries persist after
        uninstall, and pnputil requires admin to enumerate).
        """
        driver_dir = self.project_root / "bin" / "drivers" / "android"
        marker = driver_dir / self._DRIVER_MARKER

        if marker.exists():
            return SystemStatus(
                name="Bundled Android Drivers",
                present=True,
                action="ok",
                detail="Bundled OEM drivers already installed.",
            )

        has_installers = (
            driver_dir.is_dir()
            and any(
                p.suffix.lower() in (".exe", ".msi")
                for p in driver_dir.iterdir()
                if p.is_file()
            )
        )
        if not has_installers:
            return SystemStatus(
                name="Bundled Android Drivers",
                present=True,
                action="ok",
                detail="No bundled driver installers found (skipped).",
            )

        return SystemStatus(
            name="Bundled Android Drivers",
            present=False,
            action="install",
            detail="Bundled OEM drivers need to be installed.",
        )

    def _install_android_drivers(self, status: SystemStatus) -> None:
        """
        Silently run all Chimera OEM driver installers from bin/drivers/android/.

        - .exe files are run with /S (NSIS silent mode) — the most common
          packer for OEM driver installers.  Falls back to /silent, /verysilent.
        - .msi files are run via msiexec /i <file> /quiet /norestart.

        Each installer is given up to 120 seconds.  Failures are logged but
        do not abort the remaining installers.
        """
        driver_dir = self.project_root / "bin" / "drivers" / "android"
        if not driver_dir.is_dir():
            logger.error("[prereq] Driver directory not found: %s", driver_dir)
            status.action = "failed"
            status.detail = f"Driver directory not found at {driver_dir}"
            return

        installers = sorted(
            p for p in driver_dir.iterdir()
            if p.suffix.lower() in (".exe", ".msi") and p.is_file()
        )
        if not installers:
            logger.warning("[prereq] No driver installers found in %s", driver_dir)
            status.action = "failed"
            status.detail = "No .exe/.msi driver files found"
            return

        total = len(installers)
        succeeded = 0
        failed_list: list[str] = []

        print(f"\n  Installing {total} Android OEM driver(s)...")

        for i, installer in enumerate(installers, 1):
            name = installer.name
            print(f"    [{i}/{total}] {name} ...", end=" ", flush=True)

            try:
                if installer.suffix.lower() == ".msi":
                    cmd = ["msiexec", "/i", str(installer), "/quiet", "/norestart"]
                else:
                    # NSIS silent flag — works for most Chimera OEM driver EXEs
                    cmd = [str(installer), "/S"]

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=120,
                )
                if result.returncode in (0, 3010):
                    # 0 = success, 3010 = success + reboot pending
                    print("OK" + (" (reboot pending)" if result.returncode == 3010 else ""))
                    succeeded += 1
                else:
                    print(f"rc={result.returncode}")
                    failed_list.append(f"{name} (rc={result.returncode})")
                    logger.warning(
                        "[prereq] Driver installer %s returned rc=%d",
                        name, result.returncode,
                    )
            except subprocess.TimeoutExpired:
                print("TIMEOUT")
                failed_list.append(f"{name} (timeout)")
                logger.warning("[prereq] Driver installer %s timed out", name)
            except Exception as exc:
                print(f"ERROR: {exc}")
                failed_list.append(f"{name} ({exc})")
                logger.error("[prereq] Driver installer %s error: %s", name, exc)

        print(f"  Drivers: {succeeded}/{total} installed successfully.\n")

        if succeeded > 0:
            status.present = True
            status.action = "installed"
            status.detail = f"{succeeded}/{total} drivers installed"
            if failed_list:
                status.detail += f"; failed: {', '.join(failed_list)}"
            logger.info(
                "[prereq] Android drivers: %d/%d installed. Failed: %s",
                succeeded, total, failed_list or "none",
            )
            # Write marker so we don't re-run installers on every launch
            try:
                marker = driver_dir / self._DRIVER_MARKER
                marker.write_text(
                    f"{succeeded}/{total} drivers installed\n"
                    f"failed: {failed_list or 'none'}\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning("[prereq] Could not write driver marker: %s", exc)
        else:
            status.action = "failed"
            status.detail = f"All {total} driver installers failed"
            logger.error("[prereq] All Android driver installers failed")

    # ── Android USB driver store check ────────────────────────────────

    def _check_android_drivers(self) -> SystemStatus:
        """
        Informational scan of the Windows driver store (via pnputil) for
        known Android USB driver INFs.  This check does NOT gate driver
        installation — bundled Chimera drivers are handled by
        _check_bundled_drivers / _install_android_drivers instead.
        """
        found, missing, any_android = _android_drivers_in_store()

        required_present = _ANDROID_DRIVER_REQUIRED in found
        found_labels = [_ANDROID_DRIVER_INFS[f] for f in found if f in _ANDROID_DRIVER_INFS]

        if required_present or any_android:
            detail = "Found: " + ", ".join(found_labels) if found_labels else ""
            if any_android and not found_labels:
                detail = "OEM Android USB drivers detected in driver store"
            elif any_android:
                detail += " + additional OEM Android USB drivers"
            return SystemStatus(
                name="Android USB Drivers",
                present=True,
                action="ok",
                detail=detail or "Android USB drivers present",
            )
        else:
            return SystemStatus(
                name="Android USB Drivers",
                present=True,   # informational only — don't block startup
                action="ok",
                detail="No known Android drivers in driver store (bundled drivers handled separately).",
            )

    # ── Android ADB device recognition ────────────────────────────────

    def _check_adb_device(self) -> SystemStatus:
        """
        Run `adb devices` and report whether at least one device is
        recognised (not offline / unauthorized).
        """
        adb_exe = self.project_root / "bin" / "adb" / "adb.exe"
        if not adb_exe.exists():
            return SystemStatus(
                name="Android ADB",
                present=False,
                action="failed",
                detail=f"adb.exe not found at {adb_exe}",
            )

        try:
            result = subprocess.run(
                [str(adb_exe), "devices"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            lines = [
                ln.strip() for ln in result.stdout.splitlines()
                if ln.strip() and not ln.startswith("List of devices")
            ]
            recognised = [ln for ln in lines if "\tdevice" in ln]
            unauthorized = [ln for ln in lines if "unauthorized" in ln]
            offline = [ln for ln in lines if "offline" in ln]

            if recognised:
                return SystemStatus(
                    name="Android ADB",
                    present=True,
                    action="ok",
                    detail=f"{len(recognised)} device(s) ready",
                )
            if unauthorized:
                return SystemStatus(
                    name="Android ADB",
                    present=False,
                    action="prompt",
                    detail=(
                        "Android device connected but USB Debugging authorization is pending. "
                        "Check your phone screen and tap 'Allow'."
                    ),
                )
            if offline:
                return SystemStatus(
                    name="Android ADB",
                    present=False,
                    action="prompt",
                    detail=(
                        "Android device appears offline. "
                        "Try unplugging and replugging the USB cable."
                    ),
                )
            # No lines at all — nothing plugged in, which is fine at startup
            return SystemStatus(
                name="Android ADB",
                present=True,
                action="ok",
                detail="No device connected (normal at startup).",
            )
        except subprocess.TimeoutExpired:
            return SystemStatus(
                name="Android ADB",
                present=False,
                action="failed",
                detail="adb devices timed out",
            )
        except Exception as exc:
            return SystemStatus(
                name="Android ADB",
                present=False,
                action="failed",
                detail=str(exc),
            )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _vcredist_installed() -> bool:
    """
    Return True if VC++ 2015-2022 x64 Redistributable is installed.
    Checks the standard registry keys written by the Microsoft installer.
    """
    try:
        import winreg
        keys = [
            r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
            r"SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
        ]
        for key_path in keys:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                    installed, _ = winreg.QueryValueEx(key, "Installed")
                    if installed == 1:
                        return True
            except OSError:
                continue
    except ImportError:
        # Not on Windows — skip silently
        pass
    return False


def _apple_service_present() -> bool:
    """
    Return True if AppleMobileDeviceService is registered in Windows services.
    Uses `sc query` so no extra imports are needed.
    """
    try:
        result = subprocess.run(
            ["sc", "query", "Apple Mobile Device Service"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        pass

    # Fallback: registry check
    try:
        import winreg
        key_path = r"SYSTEM\CurrentControlSet\Services\Apple Mobile Device Service"
        winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path).Close()
        return True
    except Exception:
        return False


def _android_drivers_in_store() -> tuple[set[str], set[str], bool]:
    """
    Query the Windows driver store via ``pnputil /enum-drivers`` and return
    (found, missing, any_android):
      - found:        set of known INF base-names from _ANDROID_DRIVER_INFS
      - missing:      set of known INFs not found
      - any_android:  True if *any* Android/ADB-related driver was detected
                      (catches OEM drivers with non-standard INF names)

    Requires admin privileges — pnputil returns rc=1 and help text without.
    Returns (set(), set(all keys), False) on any failure.
    """
    all_known = set(_ANDROID_DRIVER_INFS.keys())
    # Keywords that indicate an Android USB driver (case-insensitive)
    _ANDROID_KEYWORDS = {"android", "adb", "samsung mobile", "lg united", "motorola device", "huawei hdb"}

    try:
        result = subprocess.run(
            ["pnputil", "/enum-drivers"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            logger.debug("[prereq] pnputil returned rc=%d (admin required?)", result.returncode)
            return set(), all_known, False

        found: set[str] = set()
        any_android = False

        for line in result.stdout.splitlines():
            stripped = line.strip()
            lower = stripped.lower()

            # Check Original Name lines for known INFs
            if lower.startswith("original name:"):
                inf_name = stripped.split(":", 1)[1].strip().lower()
                if inf_name in all_known:
                    found.add(inf_name)
                # Check if the INF name itself looks Android-related
                if any(kw in inf_name for kw in ("android", "adb", "samsung", "motorola", "huawei", "lg_and")):
                    any_android = True

            # Check Provider and Class Name lines for Android keywords
            if lower.startswith(("provider name:", "class name:", "original name:")):
                value = stripped.split(":", 1)[1].strip().lower()
                if any(kw in value for kw in _ANDROID_KEYWORDS):
                    any_android = True

        missing = all_known - found
        logger.debug(
            "[prereq] Android driver store scan: found=%s missing=%s any_android=%s",
            found, missing, any_android,
        )
        return found, missing, any_android

    except FileNotFoundError:
        logger.debug("[prereq] pnputil not found — skipping Android driver store check")
        return set(), all_known, False
    except subprocess.TimeoutExpired:
        logger.debug("[prereq] pnputil timed out")
        return set(), all_known, False
    except Exception as exc:
        logger.debug("[prereq] Android driver store check failed: %s", exc)
        return set(), all_known, False


def _pip_name(dist_name: str) -> str:
    """Return the pip install name for a distribution, defaulting to dist_name."""
    return _PIP_NAME.get(dist_name, dist_name)


def _version_gte(installed: str, required: str) -> bool:
    """
    Return True if *installed* >= *required* using simple numeric tuple comparison.
    Handles versions like "1.2.3", "1.2.3.post4", "1.2.3a1" by stripping
    non-numeric suffixes from each component.
    """
    if required == "0.0.0":
        return True  # any version is acceptable

    def _to_tuple(v: str) -> tuple[int, ...]:
        parts: list[int] = []
        for part in v.split(".")[:4]:
            # Keep only leading digits
            digits = ""
            for ch in part:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            try:
                parts.append(int(digits) if digits else 0)
            except ValueError:
                parts.append(0)
        return tuple(parts)

    try:
        return _to_tuple(installed) >= _to_tuple(required)
    except Exception:
        return True  # if we can't compare, assume ok
