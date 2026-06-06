"""
core/settings_manager.py

Persistent user settings for PhoneTransfer.

Settings are stored as JSON at <project_root>/settings.json and loaded once
per process.  Call save_settings() after mutating a Settings instance to
persist changes across sessions.

Usage
-----
    from core.settings_manager import get_settings, save_settings

    s = get_settings()
    s.backup_root = "/my/backups"
    save_settings(s)
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# settings.json lives at project root (two levels up from core/)
_SETTINGS_PATH = Path(__file__).parent.parent / "settings.json"

_SETTINGS_SINGLETON: Optional["Settings"] = None
_SETTINGS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Settings dataclass
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    # ── Appearance ───────────────────────────────────────────────────────────
    theme: str = "dark"
    # UI colour scheme: "dark" | "light" | "system"

    accent_color: str = "blue"
    # Button / progress bar accent: "blue" | "green" | "dark-blue"

    window_launch_mode: str = "fullscreen"
    # How to open the window: "fullscreen" | "last-used" | "minimum"

    window_last_width: int = 1280
    window_last_height: int = 720
    # Saved dimensions for "last-used" launch mode.

    # ── Storage ──────────────────────────────────────────────────────────────
    backup_root: str = ""
    # Directory where iOS backups are written.  Empty = project_root/tmp/backups.

    output_root: str = ""
    # Directory where exported/converted files land.  Empty = project_root/tmp/output.

    keep_temp_files: bool = False
    # Retain intermediate extraction artifacts after transfer completes.

    # ── iOS backup ───────────────────────────────────────────────────────────
    ios_backup_dir: str = ""
    # Override where iOS backups land (parent directory).  Empty = auto.

    ios_force_full_backup: bool = True
    # Always pass --full to pymobiledevice3 backup2 (ignore existing incremental).

    ios_backup_driver: str = "pymobiledevice3"
    # Backend used for iOS backups:
    #   "pymobiledevice3"  — default; modern iOS, falls back to idevicebackup2 on failure
    #   "libimobiledevice" — use idevicebackup2 directly (iOS 7-8 or when pmd3 fails)

    ios_auto_enable_encryption: bool = False
    # When True, automatically enable iTunes backup encryption on the device
    # before each fresh backup run, then disable it afterward.  Requires the
    # user to supply a backup password at transfer time.  Off by default so
    # the device's encryption state is never changed without explicit opt-in.

    ios_auto_decrypt_backup: bool = True
    # When True, automatically decrypt an encrypted backup immediately after
    # it is captured (requires a password to be supplied).  Disable only for
    # debugging — extractors cannot read encrypted blobs without decryption.

    ios_delete_backup_after_extract: bool = False
    # When True, delete the local iOS backup directory after all extractors
    # finish, provided Manifest.db passes a SQLite integrity check.  Frees
    # several GB of disk space at the cost of needing to re-run the full
    # device backup on the next transfer.  Never deletes a backup supplied via
    # ios_backup_dir (backup_dir_override).

    ios_auto_restore_modified_backup: bool = False
    # When True, automatically push the re-packed backup back to the
    # destination iPhone after a successful inject pass.  Restoring a backup
    # is destructive (it overwrites most app/user data), so this defaults to
    # False — the modified backup is left at cfg.temp_dir/ios_repacked/<udid>
    # for the user to inspect and restore manually via iMazing or
    # `pymobiledevice3 backup2 restore` until they explicitly opt in.

    # ── Transfer behaviour ────────────────────────────────────────────────────
    auto_select_all_categories: bool = True
    # Pre-tick every transfer category when a device pair is detected.

    reboot_ios_after_photos: bool = True
    # Send a reboot command to the iOS destination after photo/video transfer.

    show_quirk_warnings: bool = True
    # Show pre-transfer quirk checklist dialog when compatibility issues are found.

    default_transfer_mode: str = "backup"
    # "live" or "backup" — the default iOS extraction mode shown in the UI.

    skip_duplicates: bool = False
    # Skip writing a file to the destination if one with the same name already exists.

    backup_since_days: int = 0
    # Delta / incremental backup window in days.
    # 0 = full backup (all items).
    # N > 0 = only include items newer than N days ago.

    # ── Vault encryption ─────────────────────────────────────────────────────
    vault_encryption_mode: str = "ask"
    # When to encrypt the vault ZIP:
    #   "ask"    — prompt the user each time a backup starts (default)
    #   "always" — always encrypt; use vault_encryption_password if set,
    #              otherwise prompt for a password
    #   "never"  — never encrypt

    vault_encryption_password: str = ""
    # Default passphrase for vault encryption.
    # Empty = always prompt when encryption is required.
    # WARNING: stored in plain text in settings.json — use with caution.

    # ── Notifications ────────────────────────────────────────────────────────
    notify_on_completion: bool = True
    # Show a Windows toast notification when a backup or transfer finishes.

    # ── Category memory ───────────────────────────────────────────────────────
    category_memory: dict = field(default_factory=dict)
    # Maps device serial → list of checked category names from the last run.
    # Loaded automatically when a source device is selected.

    # ── Devices & companion ───────────────────────────────────────────────────
    wifi_discovery_enabled: bool = True
    # Scan for Wi-Fi companion devices on every device refresh.
    # Disable to speed up scans when the companion app is not in use.

    auto_install_companion: bool = True
    # Silently sideload / update the companion APK on connected Android devices.

    adb_path: str = ""
    # Path to a custom adb binary.  Empty = use the bundled adb.exe.

    # ── Logging & debug ───────────────────────────────────────────────────────
    log_level: str = "INFO"
    # Root log level: DEBUG | INFO | WARNING | ERROR

    log_to_file: bool = False
    # Write log output to <project_root>/phonetransfer.log in addition to the GUI.

    log_file_max_mb: int = 10
    # Rotate the log file when it exceeds this size (megabytes).

    log_format: str = "text"
    # Format for the rotating log file: "text" | "json"
    # "json" emits one JSON object per line — useful for log-ingestion pipelines.


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_settings() -> Settings:
    """
    Read settings.json from disk and return a Settings instance.
    Missing keys fall back to dataclass defaults.  Never raises.
    """
    if not _SETTINGS_PATH.exists():
        logger.debug("settings.json not found — using defaults")
        return Settings()
    try:
        raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        # Only accept keys that exist on the dataclass
        valid = {f for f in Settings.__dataclass_fields__}
        filtered = {k: v for k, v in raw.items() if k in valid}
        settings = Settings(**filtered)
        if settings.vault_encryption_password:
            logger.warning(
                "settings.json contains a plaintext vault_encryption_password — "
                "consider storing it in the OS keychain instead"
            )
        return settings
    except Exception as exc:
        logger.warning("Could not load settings.json: %s — using defaults", exc)
        return Settings()


def save_settings(settings: Settings) -> None:
    """Persist *settings* to settings.json.  Thread-safe.  Never raises."""
    with _SETTINGS_LOCK:
        try:
            _SETTINGS_PATH.write_text(
                json.dumps(asdict(settings), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.debug("settings.json saved to %s", _SETTINGS_PATH)
        except Exception as exc:
            logger.error("Could not save settings.json: %s", exc)


def get_settings() -> Settings:
    """
    Return the process-wide Settings singleton, loading from disk on first call.
    Thread-safe via double-checked locking.
    """
    global _SETTINGS_SINGLETON
    if _SETTINGS_SINGLETON is None:
        with _SETTINGS_LOCK:
            if _SETTINGS_SINGLETON is None:
                _SETTINGS_SINGLETON = load_settings()
    return _SETTINGS_SINGLETON
