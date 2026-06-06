"""
ui/settings_dialog.py

Settings dialog for PhoneTransfer.  Opens as a modal CTkToplevel from the
gear (⚙) button in the main window title bar.

Tabs
----
  Appearance  — theme, accent colour, window launch mode
  Storage     — backup root, output root, iOS backup dir, keep temp files
  Transfer    — categories, iOS options, skip duplicates, quirk warnings
  Devices     — companion APK, ADB path, iOS driver
  Logging     — log level, log-to-file, max file size

Usage
-----
    from ui.settings_dialog import SettingsDialog
    dlg = SettingsDialog(parent)
    # blocks until closed; settings are saved inside the dialog on OK
"""

from __future__ import annotations

import logging
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from core.settings_manager import Settings, get_settings, save_settings

logger = logging.getLogger(__name__)

_PAD     = 12
_LABEL_W = 220   # fixed width for left-column labels so the form aligns


# ---------------------------------------------------------------------------
# Helper widgets
# ---------------------------------------------------------------------------

def _section(parent: ctk.CTkFrame, text: str) -> None:
    """Render a muted section header inside a tab frame."""
    ctk.CTkLabel(
        parent,
        text=text.upper(),
        font=ctk.CTkFont(size=10, weight="bold"),
        text_color=("gray45", "gray60"),
        anchor="w",
    ).pack(fill="x", padx=_PAD, pady=(14, 2))


def _divider(parent: ctk.CTkFrame) -> None:
    ctk.CTkFrame(parent, height=1, fg_color=("gray80", "gray25")).pack(
        fill="x", padx=_PAD, pady=(0, 4)
    )


def _row(parent: ctk.CTkFrame) -> ctk.CTkFrame:
    """Return a horizontal frame for one settings row."""
    f = ctk.CTkFrame(parent, fg_color="transparent")
    f.pack(fill="x", padx=_PAD, pady=3)
    return f


def _label(parent: ctk.CTkFrame, text: str) -> None:
    ctk.CTkLabel(
        parent,
        text=text,
        font=ctk.CTkFont(size=12),
        anchor="w",
        width=_LABEL_W,
    ).pack(side="left")


def _hint(parent: ctk.CTkFrame, text: str) -> None:
    """Small muted hint line below a row."""
    ctk.CTkLabel(
        parent,
        text=text,
        font=ctk.CTkFont(size=11),
        text_color=("gray45", "gray60"),
        anchor="w",
        wraplength=460,
        justify="left",
    ).pack(fill="x", padx=_PAD + _LABEL_W + 4, pady=(0, 2))


# ---------------------------------------------------------------------------
# SettingsDialog
# ---------------------------------------------------------------------------

class SettingsDialog(ctk.CTkToplevel):
    """
    Modal settings dialog.  Instantiating this call blocks (via wait_window)
    until the user closes the dialog.  Changes are only persisted when the
    user clicks **Save**.
    """

    def __init__(self, parent: ctk.CTk) -> None:
        super().__init__(parent)
        self.title("Settings")
        self.resizable(True, True)
        self.grab_set()

        self.minsize(540, 480)

        # Work on a mutable copy; only write to singleton on Save
        self._s: Settings = _copy_settings(get_settings())

        self._build_ui()

        self.after(0, lambda: self.state("zoomed"))

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.wait_window()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        tabs = ctk.CTkTabview(self, anchor="nw")
        tabs.grid(row=0, column=0, sticky="nsew", padx=_PAD, pady=(_PAD, 0))

        for name in ("Appearance", "Storage", "Transfer", "Devices", "Logging"):
            tabs.add(name)

        self._build_appearance(tabs.tab("Appearance"))
        self._build_storage(tabs.tab("Storage"))
        self._build_transfer(tabs.tab("Transfer"))
        self._build_devices(tabs.tab("Devices"))
        self._build_logging(tabs.tab("Logging"))

        # Footer
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=1, column=0, sticky="ew", padx=_PAD, pady=_PAD)

        ctk.CTkButton(
            footer, text="Cancel", width=90,
            fg_color=("gray75", "gray30"),
            hover_color=("gray65", "gray40"),
            text_color=("gray20", "gray90"),
            command=self._on_cancel,
        ).pack(side="right", padx=(6, 0))

        ctk.CTkButton(
            footer, text="Save", width=90,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#3BA55D", hover_color="#2E8B47",
            command=self._on_save,
        ).pack(side="right")

    # ── Tab: Appearance ────────────────────────────────────────────────────────

    def _build_appearance(self, tab: ctk.CTkFrame) -> None:
        _section(tab, "Colour scheme")
        _divider(tab)

        row = _row(tab)
        _label(row, "Theme")
        self._theme_var = ctk.StringVar(value=self._s.theme)
        ctk.CTkSegmentedButton(
            row,
            values=["dark", "light", "system"],
            variable=self._theme_var,
            width=210,
        ).pack(side="left")

        row2 = _row(tab)
        _label(row2, "Accent colour")
        self._accent_var = ctk.StringVar(value=self._s.accent_color)
        ctk.CTkSegmentedButton(
            row2,
            values=["blue", "green", "dark-blue"],
            variable=self._accent_var,
            width=210,
        ).pack(side="left")
        _hint(tab, "Accent change takes effect after restarting the app.")

        _section(tab, "Window")
        _divider(tab)

        row3 = _row(tab)
        _label(row3, "Launch mode")
        self._launch_var = ctk.StringVar(value=self._s.window_launch_mode)
        ctk.CTkSegmentedButton(
            row3,
            values=["fullscreen", "last-used", "minimum"],
            variable=self._launch_var,
            width=260,
        ).pack(side="left")
        _hint(tab, "'last-used' remembers the size from your previous session.")

    # ── Tab: Storage ───────────────────────────────────────────────────────────

    def _build_storage(self, tab: ctk.CTkFrame) -> None:
        _section(tab, "Backup location")
        _divider(tab)

        row = _row(tab)
        _label(row, "Backup root directory")
        self._backup_entry = ctk.CTkEntry(row, placeholder_text="(default: tmp/backups)")
        self._backup_entry.pack(side="left", fill="x", expand=True)
        if self._s.backup_root:
            self._backup_entry.insert(0, self._s.backup_root)
        ctk.CTkButton(
            row, text="Browse", width=70,
            command=lambda: self._browse_dir(self._backup_entry),
        ).pack(side="left", padx=(4, 0))

        row_ios = _row(tab)
        _label(row_ios, "iOS backup directory")
        self._ios_backup_entry = ctk.CTkEntry(
            row_ios, placeholder_text="(default: auto-selected drive)",
        )
        self._ios_backup_entry.pack(side="left", fill="x", expand=True)
        if self._s.ios_backup_dir:
            self._ios_backup_entry.insert(0, self._s.ios_backup_dir)
        ctk.CTkButton(
            row_ios, text="Browse", width=70,
            command=lambda: self._browse_dir(self._ios_backup_entry),
        ).pack(side="left", padx=(4, 0))
        _hint(tab, "Where iOS device backups are saved. Leave blank to auto-pick the drive with the most free space.")

        _section(tab, "Output location")
        _divider(tab)

        row2 = _row(tab)
        _label(row2, "Output root directory")
        self._output_entry = ctk.CTkEntry(row2, placeholder_text="(default: tmp/output)")
        self._output_entry.pack(side="left", fill="x", expand=True)
        if self._s.output_root:
            self._output_entry.insert(0, self._s.output_root)
        ctk.CTkButton(
            row2, text="Browse", width=70,
            command=lambda: self._browse_dir(self._output_entry),
        ).pack(side="left", padx=(4, 0))

        _section(tab, "Cleanup")
        _divider(tab)

        row3 = _row(tab)
        _label(row3, "Keep intermediate temp files")
        self._keep_temp_var = ctk.BooleanVar(value=self._s.keep_temp_files)
        ctk.CTkSwitch(row3, text="", variable=self._keep_temp_var, width=46).pack(side="left")
        _hint(tab, "Useful for debugging failed transfers.")

    # ── Tab: Transfer ──────────────────────────────────────────────────────────

    def _build_transfer(self, tab: ctk.CTkFrame) -> None:
        _section(tab, "Categories")
        _divider(tab)

        row = _row(tab)
        _label(row, "Auto-select all categories")
        self._auto_cats_var = ctk.BooleanVar(value=self._s.auto_select_all_categories)
        ctk.CTkSwitch(row, text="", variable=self._auto_cats_var, width=46).pack(side="left")

        _section(tab, "iOS")
        _divider(tab)

        row_force = _row(tab)
        _label(row_force, "Always force full backup")
        self._force_full_var = ctk.BooleanVar(value=self._s.ios_force_full_backup)
        ctk.CTkSwitch(row_force, text="", variable=self._force_full_var, width=46).pack(side="left")
        _hint(tab, "Pass --full to pymobiledevice3 every time, ignoring any existing incremental backup.")

        row2 = _row(tab)
        _label(row2, "Reboot iOS device after photo transfer")
        self._reboot_ios_var = ctk.BooleanVar(value=self._s.reboot_ios_after_photos)
        ctk.CTkSwitch(row2, text="", variable=self._reboot_ios_var, width=46).pack(side="left")

        row_enc = _row(tab)
        _label(row_enc, "Auto-enable device encryption")
        self._ios_auto_enc_var = ctk.BooleanVar(value=self._s.ios_auto_enable_encryption)
        ctk.CTkSwitch(row_enc, text="", variable=self._ios_auto_enc_var, width=46).pack(side="left")
        _hint(tab, "Enable iTunes backup encryption on the device before each backup, then restore it off afterward. "
                   "Requires a password to be entered at transfer time.")

        row_dec = _row(tab)
        _label(row_dec, "Auto-decrypt backup after capture")
        self._ios_auto_dec_var = ctk.BooleanVar(value=self._s.ios_auto_decrypt_backup)
        ctk.CTkSwitch(row_dec, text="", variable=self._ios_auto_dec_var, width=46).pack(side="left")
        _hint(tab, "Automatically decrypt an encrypted backup immediately after it is created. "
                   "Requires a password to be supplied. Disable only for debugging.")

        row_del = _row(tab)
        _label(row_del, "Delete backup after extraction")
        self._ios_del_backup_var = ctk.BooleanVar(value=self._s.ios_delete_backup_after_extract)
        ctk.CTkSwitch(row_del, text="", variable=self._ios_del_backup_var, width=46).pack(side="left")
        _hint(tab, "Delete the local iOS backup once all extractors finish (after verifying "
                   "Manifest.db integrity). Reclaims several GB of disk space. "
                   "The next transfer will need to re-run a full device backup. "
                   "Never deletes a backup you pointed to manually via the iOS backup directory setting.")

        row_restore = _row(tab)
        _label(row_restore, "Auto-restore modified backup to destination iPhone")
        self._ios_auto_restore_var = ctk.BooleanVar(value=self._s.ios_auto_restore_modified_backup)
        ctk.CTkSwitch(row_restore, text="", variable=self._ios_auto_restore_var, width=46).pack(side="left")
        _hint(tab, "After a successful inject pass, automatically push the re-packed backup "
                   "to the destination iPhone via pymobiledevice3 backup2 restore. "
                   "DESTRUCTIVE — overwrites most app and user data on the device. "
                   "Off by default; the modified backup is left at temp_dir/ios_repacked/<udid> "
                   "for manual inspection / restore via iMazing until you opt in.")

        _section(tab, "Files")
        _divider(tab)

        row_dup = _row(tab)
        _label(row_dup, "Skip duplicate files")
        self._skip_dup_var = ctk.BooleanVar(value=self._s.skip_duplicates)
        ctk.CTkSwitch(row_dup, text="", variable=self._skip_dup_var, width=46).pack(side="left")
        _hint(tab, "Check the destination before writing; skip files that already exist there.")

        _section(tab, "Compatibility")
        _divider(tab)

        row4 = _row(tab)
        _label(row4, "Show pre-transfer compatibility checklist")
        self._quirk_var = ctk.BooleanVar(value=self._s.show_quirk_warnings)
        ctk.CTkSwitch(row4, text="", variable=self._quirk_var, width=46).pack(side="left")

        _section(tab, "Notifications")
        _divider(tab)

        row_notif = _row(tab)
        _label(row_notif, "Toast notification on completion")
        self._notify_var = ctk.BooleanVar(value=self._s.notify_on_completion)
        ctk.CTkSwitch(row_notif, text="", variable=self._notify_var, width=46).pack(side="left")
        _hint(tab, "Show a Windows system notification when a backup or transfer finishes.")

    # ── Tab: Devices ───────────────────────────────────────────────────────────

    def _build_devices(self, tab: ctk.CTkFrame) -> None:
        _section(tab, "Companion app")
        _divider(tab)

        row = _row(tab)
        _label(row, "Auto-install / update companion APK")
        self._companion_var = ctk.BooleanVar(value=self._s.auto_install_companion)
        ctk.CTkSwitch(row, text="", variable=self._companion_var, width=46).pack(side="left")
        _hint(tab, "Silently sideloads or updates the companion app whenever an Android device is connected.")

        _section(tab, "Android")
        _divider(tab)

        row_adb = _row(tab)
        _label(row_adb, "Custom ADB path")
        self._adb_entry = ctk.CTkEntry(
            row_adb, placeholder_text="(default: bundled adb.exe)",
        )
        self._adb_entry.pack(side="left", fill="x", expand=True)
        if self._s.adb_path:
            self._adb_entry.insert(0, self._s.adb_path)
        ctk.CTkButton(
            row_adb, text="Browse", width=70,
            command=self._browse_adb,
        ).pack(side="left", padx=(4, 0))
        _hint(tab, "Leave blank to use the bundled adb.exe. Change takes effect immediately after saving.")

        _section(tab, "iOS driver")
        _divider(tab)

        row_drv = _row(tab)
        _label(row_drv, "iOS backup driver")
        self._ios_driver_var = ctk.StringVar(value=self._s.ios_backup_driver)
        ctk.CTkSegmentedButton(
            row_drv,
            values=["pymobiledevice3"],
            variable=self._ios_driver_var,
            width=200,
        ).pack(side="left")
        _hint(tab, "pymobiledevice3 supports iOS 17+ and iOS 26. Additional drivers may be added in future releases.")

    # ── Tab: Logging ───────────────────────────────────────────────────────────

    def _build_logging(self, tab: ctk.CTkFrame) -> None:
        _section(tab, "Log level")
        _divider(tab)

        row = _row(tab)
        _label(row, "Minimum log level")
        self._log_level_var = ctk.StringVar(value=self._s.log_level)
        ctk.CTkSegmentedButton(
            row,
            values=["DEBUG", "INFO", "WARNING", "ERROR"],
            variable=self._log_level_var,
            width=260,
        ).pack(side="left")

        _section(tab, "Log file")
        _divider(tab)

        row2 = _row(tab)
        _label(row2, "Write log to file")
        self._log_file_var = ctk.BooleanVar(value=self._s.log_to_file)
        ctk.CTkSwitch(row2, text="", variable=self._log_file_var, width=46).pack(side="left")
        _hint(tab, "Saved to phonetransfer.log in the project folder.")

        row3 = _row(tab)
        _label(row3, "Max log file size (MB)")
        self._log_mb_var = ctk.StringVar(value=str(self._s.log_file_max_mb))
        ctk.CTkEntry(row3, textvariable=self._log_mb_var, width=60).pack(side="left")

    # ── Actions ────────────────────────────────────────────────────────────────

    def _browse_dir(self, entry: ctk.CTkEntry) -> None:
        path = filedialog.askdirectory(title="Select folder", parent=self)
        if path:
            entry.delete(0, "end")
            entry.insert(0, path)

    def _browse_adb(self) -> None:
        path = filedialog.askopenfilename(
            title="Select adb executable",
            parent=self,
            filetypes=[("Executables", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self._adb_entry.delete(0, "end")
            self._adb_entry.insert(0, path)

    def _on_save(self) -> None:
        # Appearance
        self._s.theme              = self._theme_var.get()
        self._s.accent_color       = self._accent_var.get()
        self._s.window_launch_mode = self._launch_var.get()

        # Storage
        self._s.backup_root     = self._backup_entry.get().strip()
        self._s.output_root     = self._output_entry.get().strip()
        self._s.ios_backup_dir  = self._ios_backup_entry.get().strip()
        self._s.keep_temp_files = self._keep_temp_var.get()

        # Transfer
        self._s.auto_select_all_categories = self._auto_cats_var.get()
        self._s.ios_force_full_backup          = self._force_full_var.get()
        self._s.reboot_ios_after_photos        = self._reboot_ios_var.get()
        self._s.ios_auto_enable_encryption         = self._ios_auto_enc_var.get()
        self._s.ios_auto_decrypt_backup            = self._ios_auto_dec_var.get()
        self._s.ios_delete_backup_after_extract    = self._ios_del_backup_var.get()
        self._s.ios_auto_restore_modified_backup   = self._ios_auto_restore_var.get()
        self._s.skip_duplicates            = self._skip_dup_var.get()
        self._s.show_quirk_warnings        = self._quirk_var.get()
        self._s.notify_on_completion       = self._notify_var.get()

        # Devices
        self._s.auto_install_companion = self._companion_var.get()
        self._s.adb_path               = self._adb_entry.get().strip()
        self._s.ios_backup_driver      = self._ios_driver_var.get()

        # Logging
        self._s.log_level    = self._log_level_var.get()
        self._s.log_to_file  = self._log_file_var.get()
        try:
            self._s.log_file_max_mb = max(1, int(self._log_mb_var.get()))
        except ValueError:
            pass

        # Write through to singleton and persist to disk
        _apply_to_singleton(self._s)
        save_settings(self._s)

        # Apply theme immediately (accent requires restart — noted in UI)
        _theme_map = {"dark": "Dark", "light": "Light", "system": "System"}
        ctk.set_appearance_mode(_theme_map.get(self._s.theme, "Dark"))

        # Wire ADB path into the live config so new ADBManager instances pick it up
        if self._s.adb_path:
            try:
                from core.config_loader import get_config
                get_config().adb_exe = Path(self._s.adb_path)
            except Exception:
                pass

        logger.info("Settings saved.")
        self.grab_release()
        self.destroy()

    def _on_cancel(self) -> None:
        self.grab_release()
        self.destroy()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _copy_settings(s: Settings) -> Settings:
    """Return a shallow copy of *s* so the dialog mutates a private instance."""
    from dataclasses import asdict
    return Settings(**asdict(s))


def _apply_to_singleton(s: Settings) -> None:
    """Overwrite every field on the process-wide singleton in-place."""
    from dataclasses import fields
    singleton = get_settings()
    for f in fields(s):
        setattr(singleton, f.name, getattr(s, f.name))
