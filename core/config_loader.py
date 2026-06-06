"""
config_loader.py

Locates and validates all binary tool paths required by the PhoneTransfer
pipeline.  The project root is discovered by walking up from __file__ until
a directory that contains both a 'core/' subdirectory and a 'bin/'
subdirectory is found.

Provides a Config dataclass (cached singleton after first call to get_config())
with fully-resolved, validated Path objects for every external binary.

Raises FileNotFoundError at startup if any critical binary is missing so
callers learn about missing tools immediately rather than at transfer time.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Project-root discovery
# ---------------------------------------------------------------------------

def _find_project_root() -> Path:
    """
    Walk upward from this file's location until we reach a directory that
    contains both 'core/' and 'bin/' subdirectories.  That directory is the
    project root.

    Raises FileNotFoundError if no such directory is found up to the
    filesystem root.
    """
    candidate = Path(__file__).resolve().parent  # starts in core/
    while True:
        if (candidate / "core").is_dir() and (candidate / "bin").is_dir():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            raise FileNotFoundError(
                "Could not locate project root.  Expected a directory that "
                "contains both 'core/' and 'bin/' subdirectories.  "
                f"Searched up to: {candidate}"
            )
        candidate = parent


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass
class Config:
    """
    Holds fully-resolved, validated paths to every external tool used by
    PhoneTransfer, plus runtime transfer options set by the UI before each run.
    """
    project_root: Path
    adb_exe: Path
    ffmpeg_exe: Path
    libimobiledevice_dir: Path          # bin/libimobiledevice/
    idevice_bins: dict[str, Path]       # "idevicepair" -> Path(...)
    temp_dir: Path                      # project_root/tmp  (created on load)
    archive_dir: Path                   # project_root/archives  (created on load)

    # ── Runtime options (set by UI before each transfer; not validated at startup) ──

    reboot_after_ios_photos: bool = True
    # Send a reboot command to the iOS destination after all categories finish.

    transfer_mode_ios: str = "backup"
    # "backup" — extraction reads from a local MobileSync backup (default).
    # "live"   — extract from live device via AFC/backup; use only when a
    #            pre-existing backup is not available or not desired.

    backup_dir_override: Path | None = None
    # If set, BackupManager uses this directory instead of temp_dir/backups/{udid}.
    # The directory must contain a valid MobileSync backup (Manifest.plist etc.).
    # idevicebackup2 is NOT invoked when this is set.

    backup_password: str | None = None
    # Password for encrypted iTunes backups.  When set and the selected backup is
    # encrypted, BackupManager.ensure_backup_for_transfer() will decrypt it
    # in-place before extraction begins.

    apps_selected_packages: list[str] | None = None
    # Package names chosen in AppPickerDialog before an Android→Android transfer.
    # None means "transfer all third-party apps" (no picker was shown or the user
    # dismissed without making a selection).

    storage_filter_extensions: list[str] | None = None
    # File extensions chosen in FileFilterDialog before a photos/storage transfer.
    # Each entry is a lowercase string with a leading dot, e.g. ".jpg", ".mp4".
    # None means no filtering — the extractor uses its built-in default extension
    # set (images + video).  Set by the ⚙ Media Filter button in the UI.

    cancel_event: threading.Event | None = field(default=None, repr=False)
    # Set by the UI when the user requests cancellation.  Long-running background
    # operations (e.g. idevicebackup2) poll this event to interrupt early.

    skip_duplicates: bool = False
    # When True, injectors should skip writing a file if one with the same name
    # already exists at the destination path.


# ---------------------------------------------------------------------------
# idevice binary names expected to be present
# ---------------------------------------------------------------------------

_IDEVICE_TOOLS = [
    "idevice_id",
    "idevicebackup2",
    "idevicediagnostics",
    "ideviceinfo",
    "idevicepair",
    "idevicesyslog",
    "iproxy",
]

# Binaries whose absence should raise an error (vs. just a warning)
_CRITICAL_BINS = [
    "idevice_id",
    "ideviceinfo",
    "idevicepair",
]


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_config() -> Config:
    """
    Build and return the Config singleton.  Validates all critical binary
    paths and raises FileNotFoundError if any are missing.  Subsequent calls
    return the cached instance with no I/O.
    """
    root = _find_project_root()
    logger.debug("Project root resolved to: %s", root)

    # ── ADB ──────────────────────────────────────────────────────────────────
    adb_exe = root / "bin" / "adb" / "adb.exe"
    _require(adb_exe, "ADB binary")

    # ── FFmpeg ────────────────────────────────────────────────────────────────
    # Standard ffmpeg Windows bundles place the executable in a nested bin/
    # subdirectory (e.g. bin/ffmpeg/bin/ffmpeg.exe).  Fall back to the flat
    # layout (bin/ffmpeg/ffmpeg.exe) for custom/stripped builds.
    ffmpeg_exe = root / "bin" / "ffmpeg" / "bin" / "ffmpeg.exe"
    if not ffmpeg_exe.exists():
        ffmpeg_exe = root / "bin" / "ffmpeg" / "ffmpeg.exe"
    _require(ffmpeg_exe, "FFmpeg binary")

    # ── libimobiledevice ─────────────────────────────────────────────────────
    limd_dir = root / "bin" / "libimobiledevice"
    if not limd_dir.is_dir():
        raise FileNotFoundError(
            f"libimobiledevice directory not found: {limd_dir}"
        )

    idevice_bins: dict[str, Path] = {}
    for tool in _IDEVICE_TOOLS:
        exe = limd_dir / f"{tool}.exe"
        if exe.exists():
            idevice_bins[tool] = exe
            logger.debug("Found idevice tool: %s -> %s", tool, exe)
        elif tool in _CRITICAL_BINS:
            raise FileNotFoundError(
                f"Critical libimobiledevice binary missing: {exe}\n"
                "Ensure the full libimobiledevice bundle is present in "
                f"{limd_dir}"
            )
        else:
            logger.warning("Optional idevice tool not found: %s", exe)

    # ── Temp directory ────────────────────────────────────────────────────────
    temp_dir = root / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    logger.debug("Temp directory: %s", temp_dir)

    # ── Archive directory ─────────────────────────────────────────────────────
    archive_dir = root / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    logger.debug("Archive directory: %s", archive_dir)

    cfg = Config(
        project_root=root,
        adb_exe=adb_exe,
        ffmpeg_exe=ffmpeg_exe,
        libimobiledevice_dir=limd_dir,
        idevice_bins=idevice_bins,
        temp_dir=temp_dir,
        archive_dir=archive_dir,
    )
    logger.info(
        "Config loaded — root: %s | adb: %s | ffmpeg: %s | idevice tools: %d",
        root,
        adb_exe,
        ffmpeg_exe,
        len(idevice_bins),
    )

    # Soft check — warns if USB drivers are absent but does not block startup.
    _check_system_drivers()

    return cfg


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_system_drivers() -> None:
    """
    Warn (but never raise) if Android or Apple USB drivers are absent from the
    Windows Driver Store.

    Live device access for both platforms requires the appropriate kernel
    driver to be installed system-wide.  Backup-based extraction works without
    them, so this is a soft check only.

    Detection uses ``pnputil /enum-drivers`` which is available on all
    supported Windows versions and requires no elevated privileges for reading.
    """
    try:
        result = subprocess.run(
            ["pnputil", "/enum-drivers"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        store = result.stdout.lower()
    except Exception as exc:
        logger.debug("Driver Store enumeration skipped: %s", exc)
        return

    # Android ADB interface — Google USB Driver covers virtually every modern
    # Android OEM in ADB mode.  Vendor-specific INFs (Samsung ssudbus, LG
    # lgandnetadb, etc.) are extras for older / unusual devices and are NOT
    # required for ADB to function.  See prerequisite_checker._ANDROID_DRIVER_INFS
    # for the canonical breakdown.
    _ANDROID_KEYWORDS = ["android", "adbinterface", "androidusb"]
    if any(k in store for k in _ANDROID_KEYWORDS):
        logger.debug("Android USB driver detected in Driver Store.")
    else:
        logger.warning(
            "No Android USB driver detected in the Windows Driver Store. "
            "Live Android device access may fail.  Install the Google USB "
            "Driver (the only one ADB actually requires) — the OEM driver "
            "packages bundled in bin/drivers/android/ are optional fallbacks "
            "for devices Google's generic driver doesn't bind to."
        )

    # Apple USB — both the legacy usbaapl64 and the newer Apple Devices driver.
    _APPLE_KEYWORDS = ["usbaapl", "appleusb", "apple mobile device", "apple devices"]
    if any(k in store for k in _APPLE_KEYWORDS):
        logger.debug("Apple USB driver detected in Driver Store.")
    else:
        logger.warning(
            "No Apple USB driver detected in the Windows Driver Store. "
            "Live iOS device access may fail. "
            "Install iTunes or the Apple USB drivers from bin/drivers/apple_usb/ to fix this."
        )


def _require(path: Path, label: str) -> None:
    """Raise FileNotFoundError with a readable message if *path* is absent."""
    if not path.exists():
        raise FileNotFoundError(
            f"{label} not found at expected path: {path}\n"
            "Ensure the binary is present before running PhoneTransfer."
        )
    logger.debug("%s found: %s", label, path)
