"""
ui/main_window.py

PhoneTransfer main window — built with customtkinter.

Layout
------
  Top:    Two device panels (Source / Destination) with refresh button
  Middle: Category checkboxes (2-column grid) | Progress panel
  Bottom: Start / Cancel button + scrollable log

Progress panel
--------------
  - Single main progress bar (advances one step per category completed)
  - Secondary iOS Backup bar (animated, visible only during iOS extraction)

Threading model
---------------
  - Device scan  : background thread -> posts results via root.after()
  - Pipeline run : background thread -> on_meta posts via root.after()
  - All Tk widget mutations happen on the main thread only.
"""

from __future__ import annotations

import logging
import math
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk

from core.capability_loader import unsupported_categories
from core.config_loader import get_config
from core.device_detector import detect_all_devices
from core.normalization_schema import DeviceInfo
from core.pipeline_manager import PipelineManager
from core.privilege_detector import detect_ios_privileges, detect_android_privileges
from core.quirk_detector import Quirk, detect_quirks
from core.settings_manager import get_settings, save_settings
from reference.device_names import resolve_ios_model, resolve_android_name

logger = logging.getLogger(__name__)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


# ---------------------------------------------------------------------------
# Logging → GUI bridge
# ---------------------------------------------------------------------------

class _QueueLoggingHandler(logging.Handler):
    """
    Routes log records from any thread into a thread-safe queue so they
    appear in the GUI log box in real time.
    """

    def __init__(self, q: "queue.Queue[str]") -> None:
        super().__init__()
        self._q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._q.put_nowait(self.format(record))
        except Exception:
            self.handleError(record)

_APP_TITLE = "PhoneTransfer"
_PAD       = 12

_UI_CATEGORIES: list[str] = [
    "contacts",
    "contact_groups",
    "blocked",
    "sms",
    "calls",
    "voicemail",
    "photos",
    "videos",
    "ringtones",
    "voice_memos",
    "wallpaper",
    "calendar",
    "reminders",
    "notes",
    "alarms",
    "bookmarks",
    "browser_history",
    "clipboard",
    "apps",
    "whatsapp",
    "signal",
    "mail_accounts",
]

_CATEGORY_LABELS: dict[str, str] = {
    "contacts":       "Contacts",
    "contact_groups": "Contact Groups",
    "blocked":        "Blocked Numbers",
    "sms":            "Messages / SMS",
    "calls":          "Call Log",
    "voicemail":      "Voicemail",
    "photos":         "Photos",
    "videos":         "Videos",
    "ringtones":      "Ringtones",
    "voice_memos":    "Voice Memos",
    "wallpaper":      "Wallpaper",
    "calendar":       "Calendar",
    "reminders":      "Reminders",
    "notes":          "Notes",
    "alarms":         "Alarms",
    "bookmarks":      "Bookmarks",
    "browser_history": "Browser History",
    "clipboard":      "Clipboard",
    "apps":           "Apps",
    "whatsapp":       "WhatsApp",
    "signal":         "Signal",
    "mail_accounts":  "Mail Accounts",
}

# Groups shown in the scrollable category panel (order matters).
_CATEGORY_GROUPS: list[tuple[str, list[str]]] = [
    ("Communication", ["contacts", "contact_groups", "blocked", "sms", "calls", "voicemail"]),
    ("Media",         ["photos", "videos", "ringtones", "voice_memos", "wallpaper"]),
    ("Productivity",  ["calendar", "reminders", "notes", "alarms", "bookmarks"]),
    ("Apps",          ["apps", "browser_history", "clipboard"]),
    ("3rd Party",     ["whatsapp", "signal", "mail_accounts"]),
]


def _device_label(dev: DeviceInfo) -> str:
    if dev.platform == "ios":
        friendly_model = resolve_ios_model(dev.model)
    else:
        friendly_model = resolve_android_name(dev.model, getattr(dev, "brand", ""))
    transport_badge = "  [Wi-Fi]" if getattr(dev, "transport", "usb") == "wifi" else ""
    return f"{dev.name}  ({friendly_model})  {dev.platform.upper()} {dev.os_version}{transport_badge}"


class MainWindow(ctk.CTk):
    """Root window of PhoneTransfer."""

    def __init__(self, initial_devices: Optional[list[DeviceInfo]] = None) -> None:
        super().__init__()
        self.title(_APP_TITLE)
        self.minsize(960, 620)
        self._apply_icon()

        self._devices:            list[DeviceInfo] = []
        self._source_dev:         Optional[DeviceInfo] = None
        self._dest_dev:           Optional[DeviceInfo] = None
        self._transfer_thread:    Optional[threading.Thread] = None
        self._cancel_event:       threading.Event = threading.Event()
        self._log_queue:          queue.Queue = queue.Queue()
        self._ios_anim_running:   bool = False
        self._companion_checking: set[str] = set()   # serials currently being checked
        # Quirks matched for the last transfer run (used by post-transfer revert dialog)
        self._active_quirks: list[tuple[Quirk, str]] = []

        # Main bar smooth animation state
        self._bar_anim_running:    bool  = False
        self._bar_anim_start_pct:  float = 0.0
        self._bar_anim_end_pct:    float = 0.0
        self._bar_anim_start_time: float = 0.0

        # Log accumulator — written by _log(), exported via "Export Log" button
        self._log_lines: list[str] = []
        # Last failed categories — drives the "Retry Failed" button
        self._last_failed_cats: list[str] = []
        self._last_transfer_src: Optional[DeviceInfo] = None
        self._last_transfer_dst: Optional[DeviceInfo] = None

        self._initial_devices = initial_devices

        self._apply_startup_settings()
        self._build_ui()
        if initial_devices is not None:
            # Devices were pre-scanned in the terminal phase — use them directly
            # and enrich with privilege detection in the background.
            self._enrich_and_apply_prescanned(initial_devices)
        else:
            self._start_device_scan()
        self._drain_log()
        if initial_devices is None:
            self._start_db_update()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Startup settings ──────────────────────────────────────────────────────

    def _apply_startup_settings(self) -> None:
        """Apply persisted settings on launch (theme, log level, window mode, etc.)."""
        s = get_settings()

        # Theme
        _theme_map = {"dark": "Dark", "light": "Light", "system": "System"}
        ctk.set_appearance_mode(_theme_map.get(s.theme, "Dark"))

        # Window launch mode
        mode = s.window_launch_mode
        if mode == "fullscreen":
            self.after(0, lambda: self.state("zoomed"))
        elif mode == "last-used":
            w, h = max(960, s.window_last_width), max(620, s.window_last_height)
            self.geometry(f"{w}x{h}")
        # "minimum" — just use the minsize, no extra geometry call

        # ADB path override
        if s.adb_path:
            try:
                from core.config_loader import get_config
                from pathlib import Path as _Path
                get_config().adb_exe = _Path(s.adb_path)
            except Exception:
                pass

        # iOS backup directory override
        if s.ios_backup_dir:
            try:
                from core.config_loader import get_config
                from pathlib import Path as _Path
                get_config().backup_dir_override = _Path(s.ios_backup_dir)
            except Exception:
                pass

        # Skip-duplicates flag
        try:
            get_config().skip_duplicates = s.skip_duplicates
        except Exception:
            pass

        # Log level
        level = getattr(logging, s.log_level, logging.INFO)
        logging.getLogger().setLevel(level)

        if s.log_to_file:
            from logging.handlers import RotatingFileHandler
            from core.config_loader import get_config
            try:
                log_path = get_config().project_root / "phonetransfer.log"
                fh = RotatingFileHandler(
                    log_path,
                    maxBytes=s.log_file_max_mb * 1024 * 1024,
                    backupCount=3,
                    encoding="utf-8",
                )
                fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
                logging.getLogger().addHandler(fh)
            except Exception as exc:
                logger.warning("Could not set up log file: %s", exc)

    def _save_window_size(self) -> None:
        """Persist the current window dimensions for 'last-used' launch mode."""
        s = get_settings()
        if s.window_launch_mode != "last-used":
            return
        try:
            geo = self.geometry()          # "WxH+X+Y"
            wh  = geo.split("+")[0]        # "WxH"
            w, h = wh.split("x")
            s.window_last_width  = int(w)
            s.window_last_height = int(h)
            from core.settings_manager import save_settings
            save_settings(s)
        except Exception:
            pass

    # ── Window close ──────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        self._save_window_size()
        self.destroy()

    # ── Settings dialog ───────────────────────────────────────────────────────

    def _open_settings(self) -> None:
        from ui.settings_dialog import SettingsDialog
        SettingsDialog(self)
        s = get_settings()
        # Re-apply log level in case it changed
        level = getattr(logging, s.log_level, logging.INFO)
        logging.getLogger().setLevel(level)
        # Re-wire ADB path if changed
        if s.adb_path:
            try:
                get_config().adb_exe = Path(s.adb_path)
            except Exception:
                pass
        # Re-wire iOS backup dir if changed
        if s.ios_backup_dir:
            try:
                get_config().backup_dir_override = Path(s.ios_backup_dir)
            except Exception:
                pass
        # Re-wire skip-duplicates flag
        try:
            get_config().skip_duplicates = s.skip_duplicates
        except Exception:
            pass

    # ── Icon ──────────────────────────────────────────────────────────────────

    def _apply_icon(self) -> None:
        """
        Set the window icon to assets/icon.ico.
        If the file does not exist yet, generate it automatically using
        assets/generate_icon.py (requires Pillow).  Failures are silently
        ignored so the app always starts even without Pillow installed.
        """
        _ROOT = Path(__file__).parent.parent
        ico   = _ROOT / "assets" / "icon.ico"

        if not ico.exists():
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "_gen_icon", _ROOT / "assets" / "generate_icon.py"
                )
                mod = importlib.util.module_from_spec(spec)   # type: ignore[arg-type]
                spec.loader.exec_module(mod)                  # type: ignore[union-attr]
                mod.generate(ico)
            except Exception:
                logger.debug("Icon generation skipped (Pillow not installed?).")
                return

        try:
            self.iconbitmap(str(ico))
        except Exception:
            logger.debug("iconbitmap() failed — running on a platform without .ico support.")

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_top_bar()
        self._build_main_content()
        self._build_bottom_bar()
        self._build_hidden_source_opts()

    def _build_top_bar(self) -> None:
        bar = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray92", "gray14"))
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)

        # Left — title + subtitle
        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=_PAD, pady=10)
        ctk.CTkLabel(
            left, text="PhoneTransfer",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            left, text="  Free & open-source phone transfer",
            font=ctk.CTkFont(size=11), text_color="gray",
        ).pack(side="left")

        # Right — action buttons
        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.grid(row=0, column=2, sticky="e", padx=_PAD, pady=10)

        self._backup_btn = ctk.CTkButton(
            right, text="⬇  Backup to PC", width=140, height=32,
            font=ctk.CTkFont(size=12),
            fg_color=("gray75", "gray28"), hover_color=("gray65", "gray35"),
            text_color=("gray20", "gray90"),
            command=self._start_backup_to_pc,
        )
        self._backup_btn.pack(side="left", padx=(0, 6))

        self._restore_btn = ctk.CTkButton(
            right, text="⬆  Restore", width=110, height=32,
            font=ctk.CTkFont(size=12),
            fg_color=("gray75", "gray28"), hover_color=("gray65", "gray35"),
            text_color=("gray20", "gray90"),
            command=self._start_restore_from_backup,
        )
        self._restore_btn.pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            right, text="⇄ Flip", width=72, height=32,
            font=ctk.CTkFont(size=12),
            fg_color=("gray75", "gray28"), hover_color=("gray65", "gray35"),
            text_color=("gray20", "gray90"),
            command=self._swap_devices,
        ).pack(side="left", padx=(0, 6))

        self._refresh_btn = ctk.CTkButton(
            right, text="↺ Refresh", width=90, height=32,
            font=ctk.CTkFont(size=12),
            fg_color="transparent", hover_color=("gray80", "gray25"),
            text_color=("gray30", "gray70"),
            command=self._start_device_scan,
        )
        self._refresh_btn.pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            right, text="📄", width=34, height=32,
            font=ctk.CTkFont(size=14),
            fg_color="transparent", hover_color=("gray80", "gray25"),
            command=self._export_log,
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            right, text="⚙", width=34, height=32,
            font=ctk.CTkFont(size=16),
            fg_color="transparent", hover_color=("gray80", "gray25"),
            command=self._open_settings,
        ).pack(side="left")

    def _build_main_content(self) -> None:
        mid = ctk.CTkFrame(self, fg_color="transparent")
        mid.grid(row=1, column=0, sticky="nsew", padx=_PAD, pady=(8, 4))
        mid.grid_columnconfigure(0, weight=1)
        mid.grid_columnconfigure(1, weight=0)
        mid.grid_columnconfigure(2, weight=1)
        mid.grid_rowconfigure(0, weight=1)

        self._src_panel = PhonePanel(
            mid, label="SOURCE", on_device_change=self._on_source_change,
        )
        self._src_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        self._build_category_panel(mid)

        self._dest_panel = PhonePanel(
            mid, label="DESTINATION", on_device_change=self._on_dest_change,
        )
        self._dest_panel.grid(row=0, column=2, sticky="nsew", padx=(6, 0))

    def _build_category_panel(self, parent) -> None:
        cat_outer = ctk.CTkFrame(parent, corner_radius=8)
        cat_outer.grid(row=0, column=1, sticky="nsew")
        cat_outer.grid_rowconfigure(1, weight=1)
        cat_outer.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            cat_outer, text="WHAT TO TRANSFER",
            font=ctk.CTkFont(size=11, weight="bold"), text_color="gray",
        ).grid(row=0, column=0, sticky="w", padx=_PAD, pady=(_PAD, 2))

        cat_scroll = ctk.CTkScrollableFrame(
            cat_outer, corner_radius=0, fg_color="transparent", width=220,
        )
        cat_scroll.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        cat_scroll.grid_columnconfigure(0, weight=1)

        self._cat_vars: dict[str, ctk.BooleanVar] = {}
        self._cat_checkboxes: dict[str, ctk.CTkCheckBox] = {}

        for group_label, cats in _CATEGORY_GROUPS:
            ctk.CTkLabel(
                cat_scroll,
                text=f"  {group_label}",
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color="gray",
                anchor="w",
            ).pack(fill="x", padx=4, pady=(8, 2))
            for cat in cats:
                var = ctk.BooleanVar(value=True)
                self._cat_vars[cat] = var
                cb = ctk.CTkCheckBox(
                    cat_scroll,
                    text=_CATEGORY_LABELS[cat],
                    variable=var,
                    font=ctk.CTkFont(size=13),
                )
                cb.pack(fill="x", padx=(_PAD, 4), pady=2)
                self._cat_checkboxes[cat] = cb

        sel_row = ctk.CTkFrame(cat_outer, fg_color="transparent")
        sel_row.grid(row=2, column=0, sticky="ew", padx=_PAD, pady=(2, _PAD // 2))
        ctk.CTkButton(
            sel_row, text="All", width=55, height=24,
            command=lambda: self._select_all(True),
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            sel_row, text="None", width=55, height=24,
            command=lambda: self._select_all(False),
        ).pack(side="left", padx=2)
        self._filter_btn = ctk.CTkButton(
            sel_row, text="⚙ Filter", width=72, height=24,
            font=ctk.CTkFont(size=11),
            fg_color=("gray75", "gray28"), hover_color=("gray65", "gray38"),
            text_color=("gray20", "gray90"),
            command=self._open_media_filter,
        )
        self._filter_btn.pack(side="left", padx=(6, 2))
        self._filter_active_label = ctk.CTkLabel(
            sel_row, text="", font=ctk.CTkFont(size=10), text_color="#4CA3E0",
        )
        self._filter_active_label.pack(side="left", padx=2)

    def _build_bottom_bar(self) -> None:
        bar = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray92", "gray14"))
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)

        # Progress section (left, expands)
        self._prog_frame = ctk.CTkFrame(bar, fg_color="transparent")
        self._prog_frame.grid(row=0, column=0, sticky="ew", padx=_PAD, pady=10)
        self._prog_frame.grid_columnconfigure(0, weight=1)

        self._current_cat_label = ctk.CTkLabel(
            self._prog_frame, text="Ready",
            font=ctk.CTkFont(size=13, weight="bold"), anchor="w",
        )
        self._current_cat_label.grid(row=0, column=0, sticky="ew", pady=(0, 3))

        self._main_bar = ctk.CTkProgressBar(
            self._prog_frame, height=14, corner_radius=7,
        )
        self._main_bar.set(0)
        self._main_bar.grid(row=1, column=0, sticky="ew", pady=(0, 2))

        self._main_stats_label = ctk.CTkLabel(
            self._prog_frame, text="",
            font=ctk.CTkFont(size=11), text_color="gray", anchor="w",
        )
        self._main_stats_label.grid(row=2, column=0, sticky="ew")

        # iOS bar — hidden until an iOS extraction starts
        self._ios_frame = ctk.CTkFrame(
            self._prog_frame, corner_radius=6, fg_color=("gray85", "gray20"),
        )
        self._ios_frame.grid_columnconfigure(0, weight=1)
        # Not gridded yet; _show_ios_bar() places it at row=3

        ctk.CTkLabel(
            self._ios_frame,
            text="iOS Backup / Decryption",
            font=ctk.CTkFont(size=11, weight="bold"), text_color="gray",
        ).grid(row=0, column=0, sticky="w", padx=_PAD, pady=(_PAD // 2, 2))

        self._ios_status_label = ctk.CTkLabel(
            self._ios_frame, text="Reading backup…",
            font=ctk.CTkFont(size=11), anchor="w",
        )
        self._ios_status_label.grid(row=1, column=0, sticky="ew",
                                     padx=_PAD, pady=(0, 2))

        self._ios_bar = ctk.CTkProgressBar(
            self._ios_frame, height=10, corner_radius=5,
            progress_color="#F0A500",
        )
        self._ios_bar.set(0)
        self._ios_bar.grid(row=2, column=0, sticky="ew",
                            padx=_PAD, pady=(0, _PAD // 2))

        # Buttons + status (right side)
        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.grid(row=0, column=1, sticky="e", padx=_PAD, pady=10)

        self._start_btn = ctk.CTkButton(
            right, text="Start Transfer", width=160,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._start_transfer,
        )
        self._start_btn.pack(side="left", padx=(0, 8))

        self._cancel_btn = ctk.CTkButton(
            right, text="Stop", width=70,
            fg_color="#555", hover_color="#E05252",
            command=self._cancel_transfer, state="disabled",
        )
        self._cancel_btn.pack(side="left", padx=(0, 8))

        self._retry_btn = ctk.CTkButton(
            right, text="⟳ Retry Failed", width=130,
            font=ctk.CTkFont(size=12),
            fg_color=("gray75", "gray28"), hover_color=("gray65", "gray35"),
            text_color=("gray20", "gray90"),
            command=self._retry_failed, state="disabled",
        )
        self._retry_btn.pack(side="left")

        self._dry_run_var = ctk.BooleanVar(value=False)
        self._dry_run_chk = ctk.CTkCheckBox(
            right, text="Dry run", variable=self._dry_run_var,
            font=ctk.CTkFont(size=12),
        )
        self._dry_run_chk.pack(side="left", padx=(12, 0))

        # Scan / status labels in a second row of the bottom bar
        self._scan_label = ctk.CTkLabel(
            bar, text="", text_color="gray", font=ctk.CTkFont(size=11),
        )
        self._scan_label.grid(row=1, column=0, sticky="w", padx=_PAD, pady=(0, 4))

        self._status_label = ctk.CTkLabel(
            bar, text="", text_color="gray", font=ctk.CTkFont(size=11),
        )
        self._status_label.grid(row=1, column=1, sticky="e", padx=_PAD, pady=(0, 4))

    def _build_hidden_source_opts(self) -> None:
        """
        Hidden frames that workers read StringVars from — never shown in the UI.
        Kept so all existing worker code (_backup_worker, _transfer_worker, etc.)
        can read self._backup_dir_var / self._backup_pw_var / self._transfer_mode_var
        without changes.
        """
        self._source_opts_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._backup_opts_inner = ctk.CTkFrame(
            self._source_opts_frame, fg_color="transparent",
        )
        self._transfer_mode_var = ctk.StringVar(value="From Backup")
        self._transfer_mode_btn = ctk.CTkSegmentedButton(
            self._source_opts_frame,
            values=["From Backup"],
            variable=self._transfer_mode_var,
            command=self._on_transfer_mode_change,
        )
        self._backup_dir_var = ctk.StringVar(value="")
        self._backup_pw_var  = ctk.StringVar(value="")

    # ── Device scanning ───────────────────────────────────────────────────────

    def _enrich_and_apply_prescanned(self, devices: list[DeviceInfo]) -> None:
        """
        Accept devices that were pre-scanned during the terminal startup phase.
        Runs privilege detection in the background, then applies results to the UI.
        """
        self._scan_label.configure(text="Enriching pre-scanned devices...")
        self._refresh_btn.configure(state="disabled")

        def _worker() -> None:
            enriched: list[DeviceInfo] = []
            for dev in devices:
                try:
                    if dev.platform == "ios":
                        jb = detect_ios_privileges(dev.udid)
                        dev = DeviceInfo(
                            udid=dev.udid, platform=dev.platform,
                            model=dev.model, name=dev.name,
                            os_version=dev.os_version,
                            is_jailbroken=jb.is_jailbroken,
                            is_rooted=False, serial=dev.serial,
                        )
                    else:
                        ri = detect_android_privileges(dev.serial)
                        dev = DeviceInfo(
                            udid=dev.udid, platform=dev.platform,
                            model=dev.model, name=dev.name,
                            os_version=dev.os_version,
                            is_jailbroken=False, is_rooted=ri.is_rooted,
                            serial=dev.serial,
                        )
                except Exception:
                    pass
                enriched.append(dev)
            self.after(0, self._on_scan_done, enriched)

        threading.Thread(target=_worker, daemon=True).start()

    def _start_device_scan(self) -> None:
        self._refresh_btn.configure(state="disabled")
        self._scan_label.configure(text="Scanning for devices...")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self) -> None:
        try:
            devices = detect_all_devices()
            enriched: list[DeviceInfo] = []
            for dev in devices:
                try:
                    if dev.platform == "ios":
                        jb = detect_ios_privileges(dev.udid)
                        dev = DeviceInfo(
                            udid=dev.udid, platform=dev.platform,
                            model=dev.model, name=dev.name,
                            os_version=dev.os_version,
                            is_jailbroken=jb.is_jailbroken,
                            is_rooted=False, serial=dev.serial,
                        )
                    else:
                        ri = detect_android_privileges(dev.serial)
                        dev = DeviceInfo(
                            udid=dev.udid, platform=dev.platform,
                            model=dev.model, name=dev.name,
                            os_version=dev.os_version,
                            is_jailbroken=False, is_rooted=ri.is_rooted,
                            serial=dev.serial,
                        )
                except Exception:
                    pass
                enriched.append(dev)

            # Wi-Fi discovery — append companion devices visible on the LAN.
            # Runs with a short timeout so USB-only users see minimal delay.
            # Skipped entirely if wifi_discovery_enabled is False in settings.
            try:
                if not get_settings().wifi_discovery_enabled:
                    raise StopIteration  # jump to except to skip cleanly
                from core.wifi_discovery import discover_companions
                wifi_devs = discover_companions(timeout=3.0)

                # Collect WiFi IPs and normalised model names of USB-connected
                # Android devices so we can skip companion entries that are
                # already represented by a USB connection.
                usb_wifi_ips: set[str] = set()
                usb_names_norm: set[str] = set()
                for d in enriched:
                    if d.platform == "android":
                        # Normalise model/name: lowercase, strip spaces & punctuation
                        for s in (d.model, d.name):
                            if s:
                                usb_names_norm.add(re.sub(r"[\s._\-]", "", s).lower())
                        # Try to get the device's WiFi IP via `ip route`
                        # (more reliable than getprop on Android 9+)
                        try:
                            from core.adb_manager import ADBManager as _ADB
                            _adb = _ADB()
                            ip_out, _, _ = _adb.shell(
                                d.serial,
                                "ip route show dev wlan0 2>/dev/null",
                                timeout=5,
                            )
                            for _line in ip_out.splitlines():
                                _m = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", _line)
                                if _m:
                                    usb_wifi_ips.add(_m.group(1))
                                    break
                        except Exception:
                            pass

                for wd in wifi_devs:
                    # Skip if this IP belongs to an already-USB-connected device
                    if wd.host in usb_wifi_ips:
                        logger.debug(
                            "wifi_discovery: skipping %s (%s) — IP matches USB device",
                            wd.name, wd.host,
                        )
                        continue
                    # Skip if the mDNS service name matches a USB device model/name
                    mdns_name = wd.name.replace("._phonetransfer._tcp.local.", "").strip()
                    if re.sub(r"[\s._\-]", "", mdns_name).lower() in usb_names_norm:
                        logger.debug(
                            "wifi_discovery: skipping %s — name matches USB device",
                            wd.name,
                        )
                        continue
                    wifi_dev = DeviceInfo(
                        udid         = wd.host,
                        platform     = "android",
                        model        = wd.properties.get("model", "Android"),
                        name         = wd.name,
                        os_version   = wd.properties.get("os", ""),
                        is_jailbroken= False,
                        is_rooted    = False,
                        serial       = wd.host,
                        transport    = "wifi",
                        wifi_host    = wd.host,
                        wifi_port    = wd.port,
                    )
                    enriched.append(wifi_dev)
            except Exception as exc:
                logger.debug("Wi-Fi discovery error (non-fatal): %s", exc)

            self.after(0, self._on_scan_done, enriched)
        except Exception as exc:
            self.after(0, self._on_scan_error, str(exc))

    def _on_scan_done(self, devices: list[DeviceInfo]) -> None:
        self._devices = devices
        self._refresh_btn.configure(state="normal")
        labels = [_device_label(d) for d in devices]

        self._src_panel.set_options(labels, devices)
        self._dest_panel.set_options(labels, devices)

        if len(devices) >= 1:
            self._src_panel.select_index(0)
            self._source_dev = devices[0]
        if len(devices) >= 2:
            self._dest_panel.select_index(1)
            self._dest_dev = devices[1]
        elif len(devices) == 1:
            self._dest_panel.select_index(0)
            self._dest_dev = devices[0]

        n = len(devices)
        suffix = "s" if n != 1 else ""
        self._scan_label.configure(
            text=f"{n} device{suffix} found." if n
            else "No devices found. Connect a phone and refresh."
        )
        self._log(f"Device scan complete: {n} device(s) found.")
        for d in devices:
            badge = ("jailbroken" if d.is_jailbroken
                     else "rooted" if d.is_rooted else "standard")
            self._log(f"  {_device_label(d)}  [{badge}]")

        # Update source options visibility and category states now that we know
        # what's connected.
        self._update_source_opts(self._source_dev)
        self._update_category_states()

        # Kick off companion check for every connected Android device
        if self._source_dev:
            self._check_companion(self._source_dev, self._src_panel)
        if self._dest_dev:
            self._check_companion(self._dest_dev, self._dest_panel)

    def _on_scan_error(self, msg: str) -> None:
        self._refresh_btn.configure(state="normal")
        self._scan_label.configure(text=f"Scan error: {msg}")
        self._log(f"ERROR during device scan: {msg}")

    # ── Background database update ─────────────────────────────────────────────

    def _start_db_update(self) -> None:
        """
        Fetch updated iOS and Android device model databases in the background.
        Runs silently on a daemon thread — the UI never blocks.  On success,
        the lookup caches are cleared so the next device scan (or label render)
        picks up the fresh data automatically.
        """
        threading.Thread(target=self._db_update_worker, daemon=True).start()

    def _db_update_worker(self) -> None:
        try:
            from reference.enrich_device_lookups import enrich_all
            from reference.device_names import refresh_caches

            android_ok, ios_ok = enrich_all()

            if android_ok or ios_ok:
                refresh_caches()
                # Re-render device labels on the main thread if devices are already shown
                self.after(0, self._refresh_device_labels)
                parts = []
                if android_ok:
                    parts.append("Android")
                if ios_ok:
                    parts.append("iOS")
                logger.debug("Device DB updated: %s", ", ".join(parts))
        except Exception as exc:
            logger.debug("Background DB update failed (no network?): %s", exc)

    # ── iOS encryption detection callback ────────────────────────────────────

    def _make_password_callback(self) -> "Callable[[], Optional[str]]":
        """
        Return a thread-safe callable that shows the iOS password dialog on
        the main thread and blocks the calling (worker) thread until the user
        responds.

        Passed as ``on_password_needed`` to BackupManager / vault_manager so
        the prompt appears *after* the backup completes and encryption is
        confirmed — not speculatively before the backup starts.
        """
        def _callback() -> Optional[str]:
            result: list[Optional[str]] = [None]
            done = threading.Event()

            def _show() -> None:
                result[0] = _ask_ios_password(
                    self,
                    title="Encrypted Backup Detected",
                    heading="This backup is encrypted",
                    hint=(
                        "Enter the iTunes/Finder backup password to decrypt it. "
                        "The decrypted backup unlocks contacts, messages, health "
                        "data, and more."
                    ),
                )
                done.set()

            self.after(0, _show)
            done.wait()
            return result[0]

        return _callback

    # ── Vault: backup to PC ───────────────────────────────────────────────────

    def _start_backup_to_pc(self) -> None:
        """Export the source device to a vault ZIP chosen by the user."""
        from tkinter import filedialog
        src = self._src_panel.selected_device()
        if src is None:
            self._set_status("Select a source device first.", color="orange")
            return

        from datetime import datetime
        default_name = f"phonetransfer_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        _out_root = get_settings().output_root.strip()
        out_path = filedialog.asksaveasfilename(
            title="Save Backup As",
            defaultextension=".zip",
            filetypes=[("Vault ZIP", "*.zip"), ("All files", "*.*")],
            initialfile=default_name,
            initialdir=_out_root if _out_root else None,
        )
        if not out_path:
            return

        cats = [k for k, var in self._cat_vars.items() if var.get()]
        if not cats:
            self._set_status("Select at least one category to back up.", color="orange")
            return

        # For iOS, ask for the backup password upfront only when
        # ios_auto_enable_encryption is on — that is the only path where the
        # password must be known *before* the backup starts (so the device can
        # be temporarily encrypted).  In every other case the reactive
        # on_password_needed callback fires after the backup completes and the
        # encryption state is actually confirmed, avoiding a spurious prompt.
        if src.platform == "ios" and get_settings().ios_auto_enable_encryption:
            self._preflight_ios_password()

        self._cancel_event.clear()
        self._set_transfer_running(True)
        self._main_bar.set(0)
        self._current_cat_label.configure(text="Starting backup…")
        self._main_stats_label.configure(text="")
        self._hide_ios_bar()
        self._set_status(f"Backing up {src.name} to PC…", color="gray")
        self._log(f"Starting backup: {src.name} → {out_path}")

        threading.Thread(
            target=self._backup_worker,
            args=(src, out_path, cats),
            daemon=True,
        ).start()

    def _preflight_ios_password(self) -> Optional[str]:
        """
        Show the iOS backup password prompt on the main thread *before* the
        worker starts.  Returns the password string, or None if the user
        skipped.  The result is stored in cfg.backup_password so
        ensure_backup_for_transfer picks it up automatically.

        This replaces the old mid-transfer reactive prompt when the user
        supplies a password upfront; the reactive callback remains as a
        fallback for when the user skips here but the backup turns out to be
        encrypted anyway.
        """
        password = _ask_ios_password(
            self,
            title="iOS Backup Password",
            heading="Enter iOS backup password (optional)",
            hint=(
                "Supply your iTunes/Finder backup password now to unlock contacts, "
                "messages, call history, and health data.  Leave blank to be prompted "
                "later if the backup turns out to be encrypted."
            ),
        )
        try:
            get_config().backup_password = password or None
        except Exception:
            pass
        return password or None

    def _resolve_vault_encryption(self) -> "Optional[str]":
        """
        Thread-safe: determine vault encryption password from settings.
        Shows a dialog on the main thread if user input is needed.
        Returns the password string, or None for no encryption.
        """
        s = get_settings()
        mode = s.vault_encryption_mode

        if mode == "never":
            return None

        if mode == "always":
            if s.vault_encryption_password:
                return s.vault_encryption_password
            # No default stored — prompt for one
            result: list[Optional[str]] = [None]
            done = threading.Event()
            def _show_pw() -> None:
                result[0] = _ask_ios_password(
                    self,
                    title="Vault Encryption",
                    heading="Enter a backup password",
                    hint="This password will be required to restore from this vault.",
                )
                done.set()
            self.after(0, _show_pw)
            done.wait()
            return result[0]

        # mode == "ask" — show encrypt yes/no + password dialog
        result2: list[Optional[str]] = [None]
        done2 = threading.Event()
        def _show_ask() -> None:
            result2[0] = _ask_vault_encryption(self, get_settings().vault_encryption_password)
            done2.set()
        self.after(0, _show_ask)
        done2.wait()
        return result2[0]

    def _backup_worker(
        self, src, out_path: str, cats: list[str],
    ) -> None:
        from pathlib import Path
        from core.vault_manager import backup_device

        # First _prog call means the iOS backup phase finished; hide ios bar then.
        _ios_bar_hidden = [False]

        def _prog(cat: str, done: int, total: int) -> None:
            def _update() -> None:
                if not _ios_bar_hidden[0]:
                    _ios_bar_hidden[0] = True
                    self._hide_ios_bar()
                pct = done / max(total, 1)
                self._main_bar.set(pct)
                self._current_cat_label.configure(text=f"Backing up: {cat}…")
                self._main_stats_label.configure(text=f"Category {done} of {total}")
                self._set_status(f"Backing up: {cat} ({done}/{total})…", color="gray")
            self.after(0, _update)

        try:
            # Expose cancel event so idevicebackup2 subprocess can be interrupted
            try:
                get_config().cancel_event = self._cancel_event
            except Exception:
                pass

            # Resolve encryption and delta-backup settings before heavy work.
            enc_password = self._resolve_vault_encryption()
            _since_days = get_settings().backup_since_days
            _since_dt = None
            if _since_days and _since_days > 0:
                from datetime import datetime, timezone, timedelta
                _since_dt = datetime.now(timezone.utc) - timedelta(days=_since_days)
                self._log(f"Delta backup: only items newer than {_since_days} day(s).")

            # For iOS, show the indeterminate bar while backup runs.
            # Once tqdm progress arrives, switch to showing real % on main bar.
            if src.platform == "ios":
                self.after(0, self._show_ios_bar, "Creating iOS backup…")

            def _on_backup_pct(pct: float, eta: str) -> None:
                def _upd() -> None:
                    if not _ios_bar_hidden[0]:
                        _ios_bar_hidden[0] = True
                        self._hide_ios_bar()
                    self._main_bar.set(pct / 100.0)
                    self._current_cat_label.configure(
                        text=f"Creating iOS backup…  {pct:.1f}%"
                    )
                    self._main_stats_label.configure(text=f"ETA: {eta}")
                self.after(0, _upd)

            summary = backup_device(
                source=src,
                output_path=Path(out_path),
                categories=cats,
                on_progress=_prog,
                on_backup_progress=_on_backup_pct if src.platform == "ios" else None,
                on_password_needed=self._make_password_callback() if src.platform == "ios" else None,
                since=_since_dt,
                encryption_password=enc_password,
            )
            completed = sum(1 for v in summary["categories"].values() if v["status"] == "completed")
            total_items = sum(v.get("extracted", 0) for v in summary["categories"].values())
            self.after(0, self._on_backup_done, out_path, completed, total_items)
        except Exception as exc:
            self.after(0, self._hide_ios_bar)
            self.after(0, self._stop_main_bar_anim)
            self.after(0, self._set_status, f"Backup failed: {exc}", "red")
            self.after(0, self._set_transfer_running, False)

    def _on_backup_done(self, out_path: str, completed: int, total_items: int) -> None:
        self._stop_main_bar_anim()
        self._hide_ios_bar()
        self._main_bar.set(1.0)
        self._current_cat_label.configure(text="Backup complete")
        self._main_stats_label.configure(
            text=f"{completed} categories · {total_items:,} items"
        )
        self._set_transfer_running(False)
        self._set_status(f"Backup complete — {total_items:,} items saved.", color="green")
        self._log(f"Backup written: {out_path}  ({completed} categories, {total_items} items)")
        # Save category selections for the source device
        if self._source_dev:
            self._save_category_memory(self._source_dev.serial)
        # Toast notification
        if get_settings().notify_on_completion:
            src_name = self._source_dev.name if self._source_dev else "device"
            self._fire_toast(
                "PhoneTransfer — Backup complete",
                f"{src_name}: {total_items:,} items in {completed} categories",
            )

    # ── Vault: restore from backup ────────────────────────────────────────────

    def _start_restore_from_backup(self) -> None:
        """Restore a vault ZIP into the destination device."""
        from tkinter import filedialog
        dest = self._dest_panel.selected_device()
        if dest is None:
            self._set_status("Select a destination device first.", color="orange")
            return

        _out_root = get_settings().output_root.strip()
        vault_path = filedialog.askopenfilename(
            title="Open Backup Vault",
            filetypes=[("Vault ZIP", "*.zip"), ("All files", "*.*")],
            initialdir=_out_root if _out_root else None,
        )
        if not vault_path:
            return

        self._cancel_event.clear()
        self._set_transfer_running(True)
        self._main_bar.set(0)
        self._current_cat_label.configure(text="Starting restore…")
        self._main_stats_label.configure(text="")
        self._hide_ios_bar()
        self._set_status(f"Restoring to {dest.name}…", color="gray")
        self._log(f"Starting restore: {vault_path} → {dest.name}")

        threading.Thread(
            target=self._restore_worker,
            args=(vault_path, dest),
            daemon=True,
        ).start()

    def _restore_worker(self, vault_path: str, dest) -> None:
        from pathlib import Path
        from core.vault_manager import restore_from_vault
        from core.vault_reader import VaultReader

        def _prog(cat: str, done: int, total: int) -> None:
            def _update() -> None:
                pct = done / max(total, 1)
                self._main_bar.set(pct)
                self._current_cat_label.configure(text=f"Restoring: {cat}…")
                self._main_stats_label.configure(text=f"Category {done} of {total}")
                self._set_status(f"Restoring: {cat} ({done}/{total})…", color="gray")
            self.after(0, _update)

        try:
            # Expose cancel event for long-running injectors
            try:
                get_config().cancel_event = self._cancel_event
            except Exception:
                pass

            # ── Companion permission gate (restore path) ──────────────────
            # Same gate as the transfer path — ensure the companion socket
            # is ready before restoring to a USB-connected Android device.
            if (
                dest.platform == "android"
                and getattr(dest, "transport", "usb") == "usb"
            ):
                try:
                    from core.process_restarter_android import ProcessRestarter
                    from core.adb_manager import ADBManager as _ADB
                    _restarter = ProcessRestarter(dest.serial, _ADB())
                    _restarter.ensure_forward()
                    if not _restarter.is_socket_ready():
                        _restarter.launch_main_activity()
                        self.after(
                            0, self._log,
                            "ACTION REQUIRED: Open the companion app on your "
                            "device and grant all permissions — the restore "
                            "will begin automatically once ready.",
                        )
                        self.after(
                            0, self._set_status,
                            "Waiting for companion permissions…", "orange",
                        )
                        _ready = _restarter.wait_until_socket_ready(
                            timeout_seconds=300
                        )
                        if _ready:
                            self.after(0, self._log, "Companion ready — starting restore.")
                            self.after(0, self._set_status, "Restoring…", "gray")
                        else:
                            self.after(
                                0, self._log,
                                "WARNING: Companion socket not ready after 5 min. "
                                "Proceeding — some categories may fail.",
                            )
                except Exception as _gate_exc:
                    logger.debug("Restore companion gate skipped: %s", _gate_exc)

            # Install a logging handler so inject module errors appear in
            # the GUI log (without this, logger.error() in inject modules
            # goes to the root logger but never reaches the exported log).
            _restore_log_handler = _QueueLoggingHandler(self._log_queue)
            _restore_log_handler.setFormatter(
                logging.Formatter("%(levelname)s %(name)s: %(message)s")
            )
            _restore_log_handler.setLevel(logging.INFO)
            _root_logger = logging.getLogger()
            _root_logger.addHandler(_restore_log_handler)

            # Log vault provenance before starting
            with VaultReader(Path(vault_path)) as r:
                mf = r.manifest
                self.after(0, self._log, (
                    f"Vault: source={mf.get('source_platform','?')} "
                    f"{mf.get('source_name','?')}  "
                    f"created={mf.get('created_at','?')}"
                ))

            summary = restore_from_vault(
                vault_path=Path(vault_path),
                destination=dest,
                on_progress=_prog,
            )

            # Remove the handler now that the restore is done
            _root_logger.removeHandler(_restore_log_handler)

            # ── Per-category result logging ───────────────────────────────
            # Previously only totals were logged; individual category errors
            # were silently discarded.  This is why the user could not see
            # why 13 of 15 categories failed.
            for cat, info in summary.get("categories", {}).items():
                status   = info.get("status", "?")
                loaded   = info.get("loaded", info.get("extracted", 0))
                injected_n = info.get("injected", 0)
                err      = info.get("error")
                if status == "completed":
                    self.after(
                        0, self._log,
                        f"  {cat}: OK ({injected_n}/{loaded} items)",
                    )
                elif status == "skipped":
                    self.after(
                        0, self._log,
                        f"  {cat}: SKIPPED — {err or 'unknown reason'}",
                    )
                else:
                    self.after(
                        0, self._log,
                        f"  {cat}: FAILED — {err or 'unknown error'}",
                    )

            completed = sum(1 for v in summary["categories"].values() if v["status"] == "completed")
            injected  = sum(v.get("injected", 0) for v in summary["categories"].values())
            self.after(0, self._on_restore_done, completed, injected)
        except Exception as exc:
            self.after(0, self._stop_main_bar_anim)
            self.after(0, self._set_status, f"Restore failed: {exc}", "red")
            self.after(0, self._set_transfer_running, False)

    def _on_restore_done(self, completed: int, injected: int) -> None:
        self._stop_main_bar_anim()
        self._main_bar.set(1.0)
        self._current_cat_label.configure(text="Restore complete")
        self._main_stats_label.configure(
            text=f"{completed} categories · {injected:,} items"
        )
        self._set_transfer_running(False)
        self._set_status(f"Restore complete — {injected:,} items injected.", color="green")
        self._log(f"Restore finished: {completed} categories, {injected} total items injected.")

    def _set_transfer_running(self, running: bool) -> None:
        """Enable/disable transfer controls (reused by both transfer and vault ops)."""
        state = "disabled" if running else "normal"
        self._start_btn.configure(state=state)
        self._cancel_btn.configure(state="normal" if running else "disabled")
        self._backup_btn.configure(state=state)
        self._restore_btn.configure(state=state)
        # Retry button only enabled when there are failed categories and not running
        if running:
            self._retry_btn.configure(state="disabled")
        elif self._last_failed_cats:
            self._retry_btn.configure(state="normal")
        else:
            self._retry_btn.configure(state="disabled")

    def _refresh_device_labels(self) -> None:
        """Re-populate device panel dropdowns with updated model names."""
        if not self._devices:
            return
        labels = [_device_label(d) for d in self._devices]
        self._src_panel.set_options(labels, self._devices)
        self._dest_panel.set_options(labels, self._devices)
        # Restore selections
        if self._source_dev in self._devices:
            self._src_panel.select_index(self._devices.index(self._source_dev))
        if self._dest_dev in self._devices:
            self._dest_panel.select_index(self._devices.index(self._dest_dev))

    # ── Device selection helpers ──────────────────────────────────────────────

    def _swap_devices(self) -> None:
        src_idx  = self._src_panel.selected_index()
        dest_idx = self._dest_panel.selected_index()
        if src_idx is None or dest_idx is None:
            return
        self._src_panel.select_index(dest_idx)
        self._dest_panel.select_index(src_idx)
        self._source_dev, self._dest_dev = self._dest_dev, self._source_dev
        # Re-check companion for both devices after swap
        if self._source_dev:
            self._check_companion(self._source_dev, self._src_panel)
        if self._dest_dev:
            self._check_companion(self._dest_dev, self._dest_panel)

    def _on_source_change(self, dev: Optional[DeviceInfo]) -> None:
        """Called when the user picks a different source device."""
        self._source_dev = dev
        self._update_source_opts(dev)
        self._update_category_states()
        self._check_companion(dev, self._src_panel)
        # Restore last-used category selection for this device (if any saved)
        if dev is not None:
            self._load_category_memory(dev.serial)

    def _on_dest_change(self, dev: Optional[DeviceInfo]) -> None:
        """Called when the user picks a different destination device."""
        self._dest_dev = dev
        self._check_companion(dev, self._dest_panel)
        self._update_category_states()

    def _on_transfer_mode_change(self, value: str) -> None:
        """Re-evaluate which categories should be available when transfer mode changes."""
        # Persist the user's choice so it becomes the new default.
        s = get_settings()
        s.default_transfer_mode = "backup" if value == "From Backup" else "live"
        save_settings(s)
        self._update_category_states()

    def _update_source_opts(self, dev: Optional[DeviceInfo]) -> None:
        """Keep transfer mode var in sync with the selected source platform."""
        if dev and dev.platform == "ios":
            # iOS always uses backup
            self._transfer_mode_btn.configure(values=["From Backup"])
            self._transfer_mode_var.set("From Backup")
        else:
            # Android/other: allow both modes; honour the saved default.
            self._transfer_mode_btn.configure(values=["Live Transfer", "From Backup"])
            saved = get_settings().default_transfer_mode
            self._transfer_mode_var.set("From Backup" if saved == "backup" else "Live Transfer")

    def _browse_backup_dir(self) -> None:
        """Open a directory picker and put the chosen path in the backup dir field."""
        from tkinter import filedialog
        chosen = filedialog.askdirectory(
            title="Select iOS backup directory",
            mustexist=True,
        )
        if chosen:
            self._backup_dir_var.set(chosen)

    # Categories accessible via AFC (live) on iOS without a MobileSync backup
    _IOS_LIVE_CATEGORIES: frozenset[str] = frozenset(["photos", "videos"])

    def _update_category_states(self) -> None:
        """
        Grey out category checkboxes that are unsupported for the current
        source/destination platform+version combination.

        Additionally, when an iOS source is in "Live Transfer" mode, only
        media categories (photos, videos) are allowed — all others require
        a MobileSync backup and are disabled with a lock indicator.

        Leaves all checkboxes enabled when either device is unknown.
        """
        src = self._source_dev
        dst = self._dest_dev
        if src is None or dst is None:
            for cat, widget in self._cat_checkboxes.items():
                widget.configure(state="normal", text=_CATEGORY_LABELS.get(cat, cat))
            return

        # Determine which categories are blocked by platform capability rules
        blocked = unsupported_categories(
            source_platform=src.platform,
            source_version=src.os_version or "0",
            dest_platform=dst.platform,
            dest_version=dst.os_version or "0",
            categories=list(self._cat_vars.keys()),
        )

        # When iOS source is in "Live Transfer" mode, non-media cats need a backup
        ios_live_locked: set[str] = set()
        if src.platform == "ios" and self._transfer_mode_var.get() == "Live Transfer":
            ios_live_locked = {
                cat for cat in self._cat_vars
                if cat not in self._IOS_LIVE_CATEGORIES
            }

        for cat, widget in self._cat_checkboxes.items():
            label = _CATEGORY_LABELS.get(cat, cat)
            if cat in blocked:
                widget.configure(state="disabled", text=f"{label}  ⚠")
            elif cat in ios_live_locked:
                widget.configure(state="disabled", text=f"{label}  🔒")
            else:
                widget.configure(state="normal", text=label)

        if blocked:
            self._log(
                f"Note: {len(blocked)} category/categories greyed out "
                f"(unsupported on this device pair): "
                + ", ".join(blocked.keys())
            )
        if ios_live_locked:
            self._log(
                "Note: Non-media categories require 'From Backup' mode for iOS. "
                "Switch to 'From Backup' to enable them."
            )

    def _select_all(self, value: bool) -> None:
        for var in self._cat_vars.values():
            var.set(value)

    def _open_media_filter(self) -> None:
        """Open FileFilterDialog so the user can choose transfer file types."""
        from ui.file_filter_dialog import FileFilterDialog

        try:
            cfg = get_config()
            current = cfg.storage_filter_extensions
        except Exception:
            current = None

        dlg = FileFilterDialog(self, current=current)

        if dlg.result is not None:
            try:
                cfg = get_config()
                cfg.storage_filter_extensions = dlg.result
                n = len(dlg.result)
                self._filter_active_label.configure(
                    text=f"{n} type{'s' if n != 1 else ''} active"
                )
            except Exception:
                pass
        # If cancelled, leave the filter unchanged (label stays as-is)

    # ── Companion install ─────────────────────────────────────────────────────

    def _check_companion(self, dev: Optional[DeviceInfo], panel: "PhonePanel") -> None:
        """Kick off a background companion-status check for an Android device."""
        if dev is None or dev.platform != "android":
            panel.hide_companion_status()
            return
        if dev.serial in self._companion_checking:
            return
        self._companion_checking.add(dev.serial)
        panel.show_companion_status("Checking companion app…", color="gray")
        threading.Thread(
            target=self._companion_check_worker, args=(dev, panel), daemon=True
        ).start()

    def _companion_check_worker(self, dev: DeviceInfo, panel: "PhonePanel") -> None:
        from core.companion_installer import check_status, CompanionStatus
        from core.adb_manager import ADBManager
        # WiFi devices are discovered via mDNS — companion is already running.
        if getattr(dev, "transport", "usb") == "wifi":
            self.after(0, self._on_companion_checked, dev, panel,
                       CompanionStatus.UP_TO_DATE, "Companion running (Wi-Fi)")
            return
        try:
            status, msg = check_status(dev.serial, ADBManager())
        except Exception as exc:
            status, msg = None, f"Check failed: {exc}"
        self.after(0, self._on_companion_checked, dev, panel, status, msg)

    def _on_companion_checked(
        self, dev: DeviceInfo, panel: "PhonePanel", status, msg: str
    ) -> None:
        from core.companion_installer import CompanionStatus
        self._companion_checking.discard(dev.serial)

        if status == CompanionStatus.UP_TO_DATE:
            panel.show_companion_status(f"✓ {msg}", color="green")
        elif status in (CompanionStatus.NOT_INSTALLED, CompanionStatus.UPDATE_AVAILABLE):
            if get_settings().auto_install_companion:
                # Silent auto-install — no user prompt
                panel.show_companion_status("Installing companion app…", color="gray")
                self._log(f"Auto-installing companion APK on {dev.name} ({dev.serial})…")
                threading.Thread(
                    target=self._companion_install_worker, args=(dev, panel), daemon=True
                ).start()
            else:
                action = "Install" if status == CompanionStatus.NOT_INSTALLED else "Update"
                panel.show_companion_status(
                    f"⚠ {msg}", color="orange",
                    btn_text=f"{action} Companion",
                    btn_cmd=lambda d=dev, p=panel: self._install_companion(d, p),
                )
        else:
            # APK_MISSING or check error
            panel.show_companion_status(f"⚠ {msg}", color="gray")

    def _install_companion(self, dev: DeviceInfo, panel: "PhonePanel") -> None:
        panel.show_companion_status("Installing companion app…", color="gray")
        self._log(f"Sideloading companion APK to {dev.name} ({dev.serial})…")
        threading.Thread(
            target=self._companion_install_worker, args=(dev, panel), daemon=True
        ).start()

    def _companion_install_worker(self, dev: DeviceInfo, panel: "PhonePanel") -> None:
        from core.companion_installer import install_companion
        from core.adb_manager import ADBManager
        # WiFi devices are already running — nothing to install via ADB.
        if getattr(dev, "transport", "usb") == "wifi":
            self.after(0, self._on_companion_installed, dev, panel,
                       True, "Companion running (Wi-Fi)")
            return
        try:
            ok, msg = install_companion(dev.serial, ADBManager())
        except Exception as exc:
            ok, msg = False, str(exc)
        self.after(0, self._on_companion_installed, dev, panel, ok, msg)

    def _on_companion_installed(
        self, dev: DeviceInfo, panel: "PhonePanel", ok: bool, msg: str
    ) -> None:
        self._log(f"  {'✓' if ok else '✗'} {msg}")
        if ok:
            panel.show_companion_status(
                f"✓ {msg} — open app on device to grant permissions", color="orange"
            )
            # Launch MainActivity immediately after install so the user sees
            # the permission-grant screen right away instead of having to wait
            # until the transfer starts.
            threading.Thread(
                target=self._launch_companion_activity,
                args=(dev,),
                daemon=True,
            ).start()
        else:
            panel.show_companion_status(
                f"✗ {msg}", color="red",
                btn_text="Retry",
                btn_cmd=lambda d=dev, p=panel: self._install_companion(d, p),
            )

    def _launch_companion_activity(self, dev: DeviceInfo) -> None:
        """Background task: open companion MainActivity so user sees permission screen."""
        if getattr(dev, "transport", "usb") != "usb":
            return
        try:
            from core.process_restarter_android import ProcessRestarter
            from core.adb_manager import ADBManager
            restarter = ProcessRestarter(dev.serial, ADBManager())
            restarter.ensure_forward()
            restarter.launch_main_activity()
            self.after(
                0, self._log,
                "ACTION REQUIRED: Open the companion app on your device "
                "and grant all permissions before starting the transfer.",
            )
        except Exception as exc:
            logger.debug("_launch_companion_activity failed: %s", exc)

    # ── Transfer ──────────────────────────────────────────────────────────────

    def _start_transfer(self) -> None:
        self._source_dev = self._src_panel.selected_device()
        self._dest_dev   = self._dest_panel.selected_device()

        if self._source_dev is None or self._dest_dev is None:
            self._set_status("No device selected.", color="red")
            return

        cats = [cat for cat, var in self._cat_vars.items() if var.get() and
                self._cat_checkboxes[cat].cget("state") != "disabled"]
        if not cats:
            self._set_status("Select at least one category.", color="red")
            return

        # If 'apps' is selected and source is Android, show the app picker.
        if "apps" in cats and self._source_dev and self._source_dev.platform == "android":
            from ui.app_picker_dialog import AppPickerDialog
            picker = AppPickerDialog(self, serial=self._source_dev.serial)
            if not picker.result:
                # User cancelled — remove apps from the run silently
                cats = [c for c in cats if c != "apps"]
                self._log("App picker cancelled — skipping apps category.")
                if not cats:
                    self._set_status("No categories selected.", color="red")
                    return
            else:
                self._log(f"Apps selected: {len(picker.result)} package(s)")
        else:
            picker = None  # no picker shown

        # ── Device compatibility check ─────────────────────────────────────────
        # Detect known quirks for the selected device pair and show a
        # pre-flight checklist if anything applies (and the setting is enabled).
        try:
            from ui.quirk_checklist_dialog import QuirkChecklistDialog
            quirk_pairs = detect_quirks(self._source_dev, self._dest_dev)
            if quirk_pairs and get_settings().show_quirk_warnings:
                src_lbl  = _device_label(self._source_dev)
                dest_lbl = _device_label(self._dest_dev)
                dlg = QuirkChecklistDialog(
                    self,
                    pairs=quirk_pairs,
                    source_label=src_lbl,
                    dest_label=dest_lbl,
                )
                if not dlg.result:
                    self._set_status("Transfer cancelled.", color="gray")
                    return
            self._active_quirks = quirk_pairs
        except Exception:
            self._active_quirks = []

        # For iOS source, ask for backup password upfront so auto-enable-encryption
        # and auto-decrypt can operate without a mid-transfer dialog.
        if self._source_dev.platform == "ios":
            self._preflight_ios_password()

        # Apply runtime transfer options to the config singleton before the run.
        try:
            cfg = get_config()
            mode = self._transfer_mode_var.get()
            cfg.transfer_mode_ios = "backup" if mode == "From Backup" else "live"
            # backup_password already set by _preflight_ios_password; leave it
            # as-is for iOS.  For non-iOS sources clear it so stale values
            # from a previous iOS run don't accidentally carry over.
            if self._source_dev.platform != "ios":
                cfg.backup_password = None
            # Store selected app packages so extract_apps_android can read them
            cfg.apps_selected_packages = picker.result if picker else None
        except Exception:
            pass  # config unavailable — proceed with defaults

        # ── Auto-restore warning for iOS destination ───────────────────────
        # Always shown when auto-restore is on — the user must confirm before
        # we push a modified backup to a real device.
        if (
            self._dest_dev.platform == "ios"
            and get_settings().ios_auto_restore_modified_backup
        ):
            import tkinter.messagebox as _mbox
            _dest_name = self._dest_dev.name
            _proceed = _mbox.askyesno(
                "Confirm Auto-Restore",
                f"Auto-restore is enabled.\n\n"
                f"After the injection pass, the modified backup will be "
                f"automatically restored to {_dest_name}. This resets most "
                f"app and user data on the device.\n\n"
                f"The restore is additive (existing data is not wiped) but "
                f"it re-applies app state and may trigger an iCloud re-sync.\n\n"
                f"Note: the backup→restore round-trip has not been smoke-tested "
                f"on a real device. Treat auto-restore as experimental.\n\n"
                f"Continue with auto-restore?",
                parent=self,
                icon="warning",
            )
            if not _proceed:
                self._set_status("Transfer cancelled.", color="gray")
                return

        # ── Pre-flight scan: estimate content sizes and check free space ────
        try:
            from core.pipeline_manager import PipelineManager
            _pm = PipelineManager(self._source_dev, self._dest_dev, categories=cats)
            scan = _pm.preflight_scan()
            total_items = scan.get("total_items", 0)
            total_bytes = scan.get("total_bytes", 0)
            dest_free   = scan.get("dest_free_bytes")

            def _fmt_size(b: int) -> str:
                if b >= 1_000_000_000:
                    return f"{b / 1_000_000_000:.1f} GB"
                if b >= 1_000_000:
                    return f"{b / 1_000_000:.0f} MB"
                return f"{b / 1_000:.0f} KB"

            scan_lines: list[str] = []
            for cat, info in scan.get("categories", {}).items():
                c = info.get("count", 0)
                if c > 0:
                    scan_lines.append(
                        f"  {cat.replace('_', ' ').title()}: ~{c:,} items ({_fmt_size(info.get('estimated_bytes', 0))})"
                    )

            if scan_lines:
                self._log("Pre-flight scan:")
                for ln in scan_lines:
                    self._log(ln)
                self._log(f"  Total: ~{total_items:,} items ({_fmt_size(total_bytes)})")
                if dest_free is not None:
                    self._log(f"  Destination free space: {_fmt_size(dest_free)}")
                    if total_bytes > 0 and total_bytes > dest_free * 0.9:
                        import tkinter.messagebox as mbox
                        proceed = mbox.askyesno(
                            "Low Space Warning",
                            f"Estimated transfer size ({_fmt_size(total_bytes)}) "
                            f"may exceed available space ({_fmt_size(dest_free)}).\n\n"
                            "Continue anyway?",
                            parent=self,
                        )
                        if not proceed:
                            self._set_status("Transfer cancelled — low space.", color="red")
                            return
        except Exception as exc:
            self._log(f"Pre-flight scan skipped: {exc}")

        # Reset progress UI
        self._main_bar.set(0)
        self._current_cat_label.configure(text="Starting…")
        self._main_stats_label.configure(text="")
        self._hide_ios_bar()

        # ── Resume check ─────────────────────────────────────────────────────
        _resume_session_id: str | None = None
        try:
            _rsid = PipelineManager.find_resumable_session(
                self._source_dev, self._dest_dev
            )
            if _rsid is not None:
                import tkinter.messagebox as _mbox
                _resume = _mbox.askyesno(
                    "Resume Previous Transfer",
                    "An incomplete transfer for this device pair was found.\n\n"
                    "Resume it (skip already-completed categories)?",
                    parent=self,
                )
                if _resume:
                    _resume_session_id = _rsid
        except Exception:
            pass

        self._cancel_event.clear()
        self._last_failed_cats = []   # clear stale retry state on new full run
        self._start_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")
        self._retry_btn.configure(state="disabled")
        self._set_status("Transferring...", color="gray")

        self._log("=" * 60)
        self._log(
            f"Transfer: {_device_label(self._source_dev)}"
            f"  ->  {_device_label(self._dest_dev)}"
        )
        self._log(f"Categories: {', '.join(cats)}")
        if _resume_session_id:
            self._log(f"Resuming session {_resume_session_id}")

        self._transfer_thread = threading.Thread(
            target=self._transfer_worker,
            args=(self._source_dev, self._dest_dev, cats, _resume_session_id),
            daemon=True,
        )
        self._transfer_thread.start()

    def _transfer_worker(
        self,
        source: DeviceInfo,
        dest: DeviceInfo,
        categories: list[str],
        resume_session_id: str | None = None,
    ) -> None:
        def on_meta(meta: dict) -> None:
            self.after(0, self._update_meta, meta)

        try:
            # Expose the cancel event on the config so long-running subprocess
            # operations (e.g. idevicebackup2) can poll it and abort early.
            try:
                get_config().cancel_event = self._cancel_event
            except Exception:
                pass

            def _on_backup_pct(pct: float, eta: str) -> None:
                def _upd() -> None:
                    self._main_bar.set(pct / 100.0)
                    self._current_cat_label.configure(
                        text=f"Creating iOS backup…  {pct:.1f}%"
                    )
                    self._main_stats_label.configure(text=f"ETA: {eta}")
                self.after(0, _upd)

            # ── Companion permission gate ──────────────────────────────────
            # After ADB install the companion has zero runtime permissions.
            # TransferService only opens its socket AFTER permissions are
            # granted in the companion UI.  Without this gate the PC starts
            # the transfer 30–60 s after install, before the user has had a
            # chance to open the app, and falls back to ADB-only mode
            # (contacts + SMS only).
            #
            # We:
            #   1. Ensure the ADB port forward is in place.
            #   2. Launch MainActivity so the user sees the permission screen.
            #   3. Poll port 7337 (up to 5 min) until the socket accepts.
            #   4. Proceed once ready; warn and continue if it times out.
            if (
                dest.platform == "android"
                and getattr(dest, "transport", "usb") == "usb"
            ):
                try:
                    from core.process_restarter_android import ProcessRestarter
                    from core.adb_manager import ADBManager as _ADB
                    _restarter = ProcessRestarter(dest.serial, _ADB())
                    _restarter.ensure_forward()
                    if not _restarter.is_socket_ready():
                        _restarter.launch_main_activity()
                        self.after(
                            0, self._log,
                            "ACTION REQUIRED: Open the companion app on your "
                            "device and grant all permissions — the transfer "
                            "will begin automatically once ready.",
                        )
                        self.after(
                            0, self._set_status,
                            "Waiting for companion permissions…", "orange",
                        )
                        _ready = _restarter.wait_until_socket_ready(
                            timeout_seconds=300
                        )
                        if _ready:
                            self.after(0, self._log, "Companion ready — starting transfer.")
                            self.after(0, self._set_status, "Transferring...", "gray")
                        else:
                            self.after(
                                0, self._log,
                                "WARNING: Companion socket not ready after 5 min. "
                                "Proceeding in ADB-only mode (contacts + SMS only).",
                            )
                except Exception as _gate_exc:
                    logger.debug("Companion socket gate skipped: %s", _gate_exc)

            pm = PipelineManager(source, dest, categories=categories,
                                 dry_run=self._dry_run_var.get(),
                                 resume_session_id=resume_session_id)
            pm._backup_progress_cb = _on_backup_pct
            if source.platform == "ios":
                pm._password_needed_cb = self._make_password_callback()
            summary = _run_with_progress(pm, on_meta, self._cancel_event, self._log_queue)
            self.after(0, self._on_transfer_done, summary)
        except Exception as exc:
            self.after(0, self._on_transfer_error, str(exc))

    def _update_meta(self, meta: dict) -> None:
        """Receive category-level progress events from the worker thread."""
        phase    = meta.get("phase")
        category = meta.get("category", "")
        index    = meta.get("index", 0)
        total    = meta.get("total", 1)
        label    = _CATEGORY_LABELS.get(category, category.replace("_", " ").title())

        if phase == "extract_start":
            start_pct = index / max(total, 1)
            end_pct   = (index + 1) / max(total, 1)
            self._current_cat_label.configure(text=f"Transferring: {label}…")
            self._main_stats_label.configure(text=f"Category {index + 1} of {total}")
            self._start_main_bar_anim(start_pct, end_pct)
            # Show iOS backup bar only when source is an iOS device
            if self._source_dev and self._source_dev.platform == "ios":
                self._show_ios_bar("Reading iOS backup…")

        elif phase in ("done", "error"):
            pct = index / max(total, 1)
            self._stop_main_bar_anim()
            self._main_bar.set(pct)
            self._main_stats_label.configure(
                text=f"{index} of {total} categories done"
            )
            self._hide_ios_bar()

    def _on_transfer_done(self, summary: dict) -> None:
        self._start_btn.configure(state="normal")
        self._cancel_btn.configure(state="disabled")
        self._hide_ios_bar()
        self._stop_main_bar_anim()

        if summary.get("cancelled"):
            self._main_bar.set(0.0)
            self._current_cat_label.configure(text="Transfer cancelled")
            self._main_stats_label.configure(text="")
            self._set_status("Transfer cancelled.", color="orange")
            self._log("Transfer cancelled by user.")
            return

        is_dry = self._dry_run_var.get()

        self._main_bar.set(1.0)
        self._current_cat_label.configure(
            text="Preview complete — nothing written" if is_dry else "Transfer complete"
        )

        cats      = summary.get("categories", {})
        completed = sum(1 for v in cats.values() if v["status"] == "completed")
        failed    = sum(1 for v in cats.values() if v["status"] == "failed")
        total     = len(cats)
        self._main_stats_label.configure(
            text=f"{completed} of {total} {'would transfer' if is_dry else 'succeeded'}"
            + (f"  •  {failed} failed" if failed else "")
        )

        # Store failed categories for "Retry Failed" button + remember source/dest
        self._last_failed_cats = [c for c, v in cats.items() if v["status"] == "failed"]
        self._last_transfer_src = self._source_dev
        self._last_transfer_dst = self._dest_dev

        prefix = "Dry run" if is_dry else "Transfer"
        msg = f"{prefix} done  {completed} completed, {failed} failed."
        self._set_status(msg, color="green" if not failed else "orange")

        # Save category selections for the source device
        if self._source_dev:
            self._save_category_memory(self._source_dev.serial)

        # Toast notification
        if get_settings().notify_on_completion:
            src_name = self._source_dev.name if self._source_dev else "device"
            body = f"{src_name}: {completed}/{total} categories"
            if is_dry:
                body = f"[DRY RUN] {body}"
            if failed:
                body += f"  •  {failed} failed"
            self._fire_toast("PhoneTransfer — Transfer complete", body)

        # ── Structured summary log ─────────────────────────────────────────
        SEP = "─" * 58
        self._log(SEP)
        self._log("  DRY RUN PREVIEW — no data was written" if is_dry else "  TRANSFER SUMMARY")
        self._log(SEP)
        self._log(f"  {'Category':<22} {'Extracted':>10} {'Injected':>10}  Status")
        self._log(SEP)
        total_extracted = total_injected = 0
        for cat, res in cats.items():
            label  = _CATEGORY_LABELS.get(cat, cat.replace("_", " ").title())
            status = res["status"]
            ext    = res.get("extracted", 0)
            inj    = res.get("injected", 0)
            total_extracted += ext
            total_injected  += inj
            icon = "✓" if status == "completed" else ("✗" if status == "failed" else "—")
            dropped = ext - inj
            drop_note = f"  ({dropped} dropped)" if dropped > 0 else ""
            self._log(f"  {icon} {label:<21} {ext:>10} {inj:>10}  {status}{drop_note}")
            if res.get("error"):
                self._log(f"      ! {res['error']}")
        self._log(SEP)
        self._log(f"  {'TOTAL':<22} {total_extracted:>10} {total_injected:>10}")
        self._log(SEP)

        archive = summary.get("archive_path")
        if archive:
            self._log(f"  Archive: {archive}")

        log_path = self._write_transfer_log(summary, is_dry)
        if log_path:
            self._log(f"  Log saved: {log_path}")

        # Show post-transfer revert reminder for any quirks that asked the user
        # to temporarily change settings (e.g. USB Restricted Mode, PTP mode).
        revert_pairs = [
            (q, r) for q, r in self._active_quirks if q.revert_steps
        ]
        if revert_pairs:
            try:
                from ui.quirk_checklist_dialog import RevertReminderDialog
                src_lbl  = _device_label(self._source_dev) if self._source_dev else "Source"
                dest_lbl = _device_label(self._dest_dev)   if self._dest_dev   else "Destination"
                RevertReminderDialog(
                    self,
                    pairs=revert_pairs,
                    source_label=src_lbl,
                    dest_label=dest_lbl,
                )
            except Exception:
                pass  # non-critical — transfer is complete regardless

    def _write_transfer_log(self, summary: dict, is_dry: bool) -> str | None:
        """Write a plain-text transfer summary to ~/Documents/PhoneTransfer/logs/."""
        try:
            from datetime import datetime as _dt
            import os as _os
            log_dir = Path(_os.path.expanduser("~")) / "Documents" / "PhoneTransfer" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = _dt.now().strftime("%Y%m%d_%H%M%S")
            tag = "dryrun" if is_dry else "transfer"
            log_file = log_dir / f"{tag}_{ts}.txt"
            cats = summary.get("categories", {})
            SEP = "─" * 58
            lines: list[str] = [
                SEP,
                f"  PhoneTransfer {'DRY RUN PREVIEW' if is_dry else 'TRANSFER LOG'}",
                f"  {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}",
            ]
            src = summary.get("source", {})
            dst = summary.get("destination", {})
            if src:
                lines.append(f"  Source:      {src.get('platform','').upper()} {src.get('serial','')}")
            if dst:
                lines.append(f"  Destination: {dst.get('platform','').upper()} {dst.get('serial','')}")
            lines += [SEP, f"  {'Category':<22} {'Extracted':>10} {'Injected':>10}  Status", SEP]
            total_ext = total_inj = 0
            for cat, res in cats.items():
                label = _CATEGORY_LABELS.get(cat, cat.replace("_", " ").title())
                ext   = res.get("extracted", 0)
                inj   = res.get("injected", 0)
                total_ext += ext
                total_inj += inj
                icon = "✓" if res["status"] == "completed" else ("✗" if res["status"] == "failed" else "—")
                dropped = ext - inj
                drop = f"  ({dropped} dropped)" if dropped > 0 else ""
                lines.append(f"  {icon} {label:<21} {ext:>10} {inj:>10}  {res['status']}{drop}")
                if res.get("error"):
                    lines.append(f"      ! {res['error']}")
            lines += [SEP, f"  {'TOTAL':<22} {total_ext:>10} {total_inj:>10}", SEP]
            if summary.get("archive_path"):
                lines.append(f"  Archive: {summary['archive_path']}")
            log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return str(log_file)
        except Exception:
            return None

    def _on_transfer_error(self, msg: str) -> None:
        self._start_btn.configure(state="normal")
        self._cancel_btn.configure(state="disabled")
        self._hide_ios_bar()
        self._stop_main_bar_anim()
        self._set_status(f"Transfer failed: {msg}", color="red")
        self._log(f"FATAL: {msg}")

    def _cancel_transfer(self) -> None:
        self._cancel_event.set()
        self._cancel_btn.configure(state="disabled")
        self._hide_ios_bar()
        self._stop_main_bar_anim()
        self._set_status("Cancellation requested...", color="orange")
        self._log("User requested cancellation.")

    # ── iOS bar animation ─────────────────────────────────────────────────────

    def _show_ios_bar(self, status_text: str = "Reading iOS backup…") -> None:
        self._ios_status_label.configure(text=status_text)
        self._ios_frame.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        if not self._ios_anim_running:
            self._ios_anim_running = True
            self._ios_anim_tick()

    def _hide_ios_bar(self) -> None:
        self._ios_anim_running = False
        self._ios_frame.grid_remove()

    def _ios_anim_tick(self) -> None:
        """Animate the iOS bar with a sine-wave pulse at ~25 fps."""
        if not self._ios_anim_running:
            return
        val = (math.sin(time.monotonic() * 2.5) + 1) / 2
        self._ios_bar.set(val)
        self.after(40, self._ios_anim_tick)

    # ── Main bar smooth animation ─────────────────────────────────────────────

    def _start_main_bar_anim(self, start_pct: float, end_pct: float) -> None:
        """
        Animate the main bar from *start_pct* asymptotically toward *end_pct*.

        Uses an exponential ease-out so the bar crawls quickly at first then
        slows to a near-stop — giving live feedback without ever overshooting
        before the "done" snap.  tau=45 s means ~63 % of the category slice
        is filled after 45 s, ~95 % after ~135 s.
        """
        self._bar_anim_start_pct  = start_pct
        self._bar_anim_end_pct    = end_pct
        self._bar_anim_start_time = time.monotonic()
        if not self._bar_anim_running:
            self._bar_anim_running = True
            self._main_bar_anim_tick()

    def _stop_main_bar_anim(self) -> None:
        self._bar_anim_running = False

    def _main_bar_anim_tick(self) -> None:
        if not self._bar_anim_running:
            return
        elapsed  = time.monotonic() - self._bar_anim_start_time
        # Asymptotic fill: approaches end_pct but never reaches it before snap
        progress = 1.0 - math.exp(-elapsed / 45.0)
        pct = self._bar_anim_start_pct + (
            self._bar_anim_end_pct - self._bar_anim_start_pct
        ) * progress * 0.95  # cap at 95 % of the slice so snap is visible
        self._main_bar.set(pct)
        self.after(200, self._main_bar_anim_tick)  # 5 fps — smooth but cheap

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        import time as _time
        line = f"{_time.strftime('%H:%M:%S')}  {msg}"
        self._log_lines.append(line)
        logger.info(msg)

    def _export_log(self) -> None:
        """Save accumulated session log to a user-chosen .txt file."""
        from tkinter import filedialog
        import time as _time
        default = f"phonetransfer_log_{_time.strftime('%Y%m%d_%H%M%S')}.txt"
        path = filedialog.asksaveasfilename(
            title="Export Log",
            defaultextension=".txt",
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")],
            initialfile=default,
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(self._log_lines))
            self._set_status(f"Log exported: {path}", color="green")
        except Exception as exc:
            self._set_status(f"Log export failed: {exc}", color="red")

    @staticmethod
    def _fire_toast(title: str, body: str) -> None:
        """Show a Windows balloon-tip notification via PowerShell (no extra deps)."""
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Information; "
            "$n.Visible = $true; "
            f"$n.ShowBalloonTip(6000, '{title}', '{body}', "
            "[System.Windows.Forms.ToolTipIcon]::Info); "
            "Start-Sleep -Seconds 7; "
            "$n.Dispose()"
        )
        try:
            kwargs: dict = {}
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.Popen(
                ["powershell", "-WindowStyle", "Hidden", "-Command", script],
                **kwargs,
            )
        except Exception:
            pass  # toast is non-critical

    def _save_category_memory(self, serial: str) -> None:
        """Persist the current checkbox state for *serial* in settings."""
        checked = [cat for cat, var in self._cat_vars.items() if var.get()]
        s = get_settings()
        s.category_memory[serial] = checked
        save_settings(s)

    def _load_category_memory(self, serial: str) -> None:
        """Restore checkbox state for *serial* from settings (if any saved)."""
        saved = get_settings().category_memory.get(serial)
        if saved is None:
            return
        saved_set = set(saved)
        for cat, var in self._cat_vars.items():
            var.set(cat in saved_set)

    def _retry_failed(self) -> None:
        """Re-run only the categories that failed in the last transfer."""
        if not self._last_failed_cats:
            return
        src = self._last_transfer_src or self._src_panel.selected_device()
        dst = self._last_transfer_dst or self._dest_panel.selected_device()
        if src is None or dst is None:
            self._set_status("Devices no longer available for retry.", color="orange")
            return

        cats = self._last_failed_cats[:]
        self._log(f"Retrying {len(cats)} failed category/categories: {', '.join(cats)}")

        # For iOS, keep the existing backup (don't re-run the full device backup).
        try:
            cfg = get_config()
            cfg.transfer_mode_ios = "backup"
            if src.platform == "ios":
                cfg.backup_password = cfg.backup_password  # keep existing value
        except Exception:
            pass

        self._cancel_event.clear()
        self._last_failed_cats = []
        self._retry_btn.configure(state="disabled")
        self._set_transfer_running(True)
        self._main_bar.set(0)
        self._current_cat_label.configure(text="Retrying failed categories…")
        self._main_stats_label.configure(text="")
        self._hide_ios_bar()
        self._set_status("Retrying…", color="gray")

        self._transfer_thread = threading.Thread(
            target=self._transfer_worker,
            args=(src, dst, cats),
            daemon=True,
        )
        self._transfer_thread.start()

    def _drain_log(self) -> None:
        # Drain the queue (fed by _run_with_progress's QueueLoggingHandler)
        # so it doesn't grow unbounded.  Output goes to the terminal via
        # the root logger; there is no on-screen log box in this layout.
        try:
            while True:
                self._log_queue.get_nowait()
        except queue.Empty:
            pass
        self.after(150, self._drain_log)

    def _set_status(self, msg: str, color: str = "gray") -> None:
        self._status_label.configure(text=msg, text_color=color)


# ── Phone panel widget ────────────────────────────────────────────────────────

class PhonePanel(ctk.CTkFrame):
    """Phone icon + info card + dropdown for one device slot."""

    def __init__(self, parent, label: str, on_device_change=None) -> None:
        super().__init__(parent, corner_radius=10)
        self._devices: list[DeviceInfo] = []
        self._on_device_change = on_device_change

        self.grid_columnconfigure(0, weight=1)

        # Header label (SOURCE / DESTINATION)
        ctk.CTkLabel(
            self, text=label,
            font=ctk.CTkFont(size=11, weight="bold"), text_color="gray",
        ).grid(row=0, column=0, sticky="w", padx=_PAD, pady=(_PAD, 0))

        # Phone icon
        self._icon_label = ctk.CTkLabel(
            self, text="📱", font=ctk.CTkFont(size=52),
        )
        self._icon_label.grid(row=1, column=0, pady=(10, 6))

        # Info card
        self._info_frame = ctk.CTkFrame(
            self, corner_radius=8, fg_color=("gray85", "gray22"),
        )
        self._info_frame.grid(row=2, column=0, sticky="ew", padx=_PAD, pady=(0, 8))
        self._info_frame.grid_columnconfigure(0, weight=1)

        self._name_label = ctk.CTkLabel(
            self._info_frame, text="No device",
            font=ctk.CTkFont(size=13, weight="bold"), anchor="w",
        )
        self._name_label.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))

        self._detail_label = ctk.CTkLabel(
            self._info_frame, text="Connect a phone and refresh",
            font=ctk.CTkFont(size=11), text_color="gray",
            anchor="w", wraplength=220, justify="left",
        )
        self._detail_label.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 8))

        # Device dropdown
        self._option_var = ctk.StringVar(value="No device")
        self._option_menu = ctk.CTkOptionMenu(
            self, variable=self._option_var,
            values=["No device"], command=self._on_select, width=280,
        )
        self._option_menu.grid(row=3, column=0, sticky="ew", padx=_PAD, pady=(0, 6))

        # Companion status row — hidden until shown
        self._companion_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._companion_label = ctk.CTkLabel(
            self._companion_frame, text="",
            font=ctk.CTkFont(size=11), anchor="w",
        )
        self._companion_label.pack(side="left", padx=(0, 6))
        self._companion_btn = ctk.CTkButton(
            self._companion_frame, text="", width=130, height=22,
            font=ctk.CTkFont(size=11),
        )

    # ── Companion status helpers ───────────────────────────────────────────

    def show_companion_status(
        self,
        text: str,
        color: str = "gray",
        btn_text: str = "",
        btn_cmd=None,
    ) -> None:
        self._companion_label.configure(text=text, text_color=color)
        if btn_text and btn_cmd:
            self._companion_btn.configure(text=btn_text, command=btn_cmd)
            if not self._companion_btn.winfo_ismapped():
                self._companion_btn.pack(side="left")
        else:
            if self._companion_btn.winfo_ismapped():
                self._companion_btn.pack_forget()
        if not self._companion_frame.winfo_ismapped():
            self._companion_frame.grid(
                row=4, column=0, sticky="ew", padx=_PAD, pady=(0, _PAD // 2),
            )

    def hide_companion_status(self) -> None:
        if self._companion_frame.winfo_ismapped():
            self._companion_frame.grid_remove()

    # ── Public interface (same as old DevicePanel) ─────────────────────────

    def set_options(self, labels: list[str], devices: list[DeviceInfo]) -> None:
        self._devices = devices
        vals = labels if labels else ["No device"]
        self._option_menu.configure(values=vals)
        if vals:
            self._option_var.set(vals[0])
        self._update_info(devices[0] if devices else None)

    def select_index(self, index: int) -> None:
        if index < len(self._devices):
            values = self._option_menu.cget("values")
            if index < len(values):
                self._option_var.set(values[index])
                self._update_info(self._devices[index])

    def selected_index(self) -> Optional[int]:
        val    = self._option_var.get()
        values = self._option_menu.cget("values")
        try:
            return list(values).index(val)
        except ValueError:
            return None

    def selected_device(self) -> Optional[DeviceInfo]:
        idx = self.selected_index()
        if idx is not None and idx < len(self._devices):
            return self._devices[idx]
        return None

    def _on_select(self, choice: str) -> None:
        values = self._option_menu.cget("values")
        dev: Optional[DeviceInfo] = None
        try:
            idx = list(values).index(choice)
            dev = self._devices[idx] if idx < len(self._devices) else None
        except ValueError:
            pass
        self._update_info(dev)
        if self._on_device_change is not None:
            self._on_device_change(dev)

    def _update_info(self, dev: Optional[DeviceInfo]) -> None:
        if dev is None:
            self._icon_label.configure(text="📱")
            self._name_label.configure(text="No device")
            self._detail_label.configure(text="Connect a phone and refresh")
            return
        icon = "📱" if dev.platform == "ios" else "🤖"
        self._icon_label.configure(text=icon)
        if dev.platform == "ios":
            friendly = resolve_ios_model(dev.model)
        else:
            friendly = resolve_android_name(dev.model, getattr(dev, "brand", ""))
        self._name_label.configure(text=dev.name or friendly)
        priv = (
            "Jailbroken" if dev.is_jailbroken
            else "Rooted" if dev.is_rooted
            else "Standard"
        )
        transport = "  [Wi-Fi]" if getattr(dev, "transport", "usb") == "wifi" else ""
        serial_short = dev.serial[:22] + ("…" if len(dev.serial) > 22 else "")
        self._detail_label.configure(
            text=(
                f"{friendly}  •  {dev.platform.upper()} {dev.os_version}{transport}\n"
                f"{priv} access  •  {serial_short}"
            )
        )


# ── iOS backup password prompt ────────────────────────────────────────────────

def _ask_ios_password(
    parent: ctk.CTk,
    title: str = "iOS Backup Password",
    heading: str = "Enter iOS backup password",
    hint: str = "Leave blank if your backup is not encrypted.",
) -> Optional[str]:
    """
    Show a small modal dialog asking for the iOS backup password.

    Returns the entered password string, or None if the user leaves it blank.
    """
    dlg = ctk.CTkToplevel(parent)
    dlg.title(title)
    dlg.resizable(False, False)
    dlg.grab_set()

    dlg.update_idletasks()
    pw, h = 380, 175
    px = parent.winfo_rootx() + (parent.winfo_width()  - pw) // 2
    py = parent.winfo_rooty() + (parent.winfo_height() - h)  // 2
    dlg.geometry(f"{pw}x{h}+{px}+{py}")

    ctk.CTkLabel(
        dlg,
        text=heading,
        font=ctk.CTkFont(size=13, weight="bold"),
    ).pack(padx=16, pady=(16, 4))

    ctk.CTkLabel(
        dlg,
        text=hint,
        font=ctk.CTkFont(size=11),
        text_color="gray",
        wraplength=340,
    ).pack(padx=16, pady=(0, 8))

    pw_var = ctk.StringVar()
    entry  = ctk.CTkEntry(dlg, textvariable=pw_var, show="*", width=320)
    entry.pack(padx=16, pady=(0, 12))
    entry.focus_set()

    result: list[Optional[str]] = [None]

    def _ok(event=None) -> None:
        result[0] = pw_var.get() or None
        dlg.grab_release()
        dlg.destroy()

    def _skip() -> None:
        dlg.grab_release()
        dlg.destroy()

    entry.bind("<Return>", _ok)
    entry.bind("<Escape>", lambda _e: _skip())

    btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
    btn_row.pack(pady=(0, 4))
    ctk.CTkButton(
        btn_row, text="Skip", command=_skip, width=100,
        fg_color=("gray75", "gray30"), hover_color=("gray65", "gray40"),
        text_color=("gray20", "gray90"),
    ).pack(side="left", padx=(0, 8))
    ctk.CTkButton(btn_row, text="Continue", command=_ok, width=120).pack(side="left")

    dlg.wait_window()
    return result[0]


def _ask_vault_encryption(
    parent: ctk.CTk,
    default_password: str = "",
) -> "Optional[str]":
    """
    Ask the user whether to encrypt this backup, and if so, for a password.

    Returns the password string if the user chose to encrypt, or None if they
    chose not to.  If *default_password* is set, it is pre-filled in the entry.
    """
    dlg = ctk.CTkToplevel(parent)
    dlg.title("Vault Encryption")
    dlg.resizable(False, False)
    dlg.grab_set()

    dlg.update_idletasks()
    pw, h = 400, 230
    px = parent.winfo_rootx() + (parent.winfo_width()  - pw) // 2
    py = parent.winfo_rooty() + (parent.winfo_height() - h)  // 2
    dlg.geometry(f"{pw}x{h}+{px}+{py}")

    ctk.CTkLabel(
        dlg,
        text="Encrypt this backup?",
        font=ctk.CTkFont(size=13, weight="bold"),
    ).pack(padx=16, pady=(16, 4))

    ctk.CTkLabel(
        dlg,
        text="An encrypted vault requires a password to restore. Leave the password\n"
             "field empty and click Skip to save without encryption.",
        font=ctk.CTkFont(size=11),
        text_color="gray",
        wraplength=360,
        justify="center",
    ).pack(padx=16, pady=(0, 10))

    pw_var = ctk.StringVar(value=default_password)
    entry = ctk.CTkEntry(dlg, textvariable=pw_var, show="*", width=340,
                         placeholder_text="Password (leave blank to skip encryption)")
    entry.pack(padx=16, pady=(0, 12))
    entry.focus_set()

    result: list["Optional[str]"] = [None]

    def _encrypt(event=None) -> None:
        result[0] = pw_var.get() or None
        dlg.grab_release()
        dlg.destroy()

    def _skip() -> None:
        result[0] = None
        dlg.grab_release()
        dlg.destroy()

    btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
    btn_row.pack()
    ctk.CTkButton(btn_row, text="Encrypt", command=_encrypt, width=120).pack(side="left", padx=6)
    ctk.CTkButton(btn_row, text="Skip", command=_skip, width=100,
                  fg_color="gray40", hover_color="gray30").pack(side="left", padx=6)
    entry.bind("<Return>", _encrypt)

    dlg.wait_window()
    return result[0]


# ── Pipeline runner with live progress ───────────────────────────────────────

def _run_with_progress(
    pm: PipelineManager,
    on_meta,
    cancel_event: threading.Event,
    log_queue: "queue.Queue[str]",
) -> dict:
    """
    Run PipelineManager, calling on_meta at each category boundary.

    on_meta receives a dict with:
      phase    — "extract_start" | "done" | "error"
      category — category name
      index    — number of categories completed so far
      total    — total categories being transferred

    All Python logging output (INFO and above) is routed to *log_queue* for
    the duration of the transfer so every logger.info() call from the pipeline
    appears in the GUI log box in real time.
    """
    from core.session_manager import SessionManager
    import core.pipeline_manager as pm_mod

    class _TransferCancelled(BaseException):
        """Raised (not Exception) so it bypasses PipelineManager's per-category
        except-Exception handler and immediately unwinds the pipeline loop."""

    # Install a logging handler that feeds records into the GUI log queue
    _handler = _QueueLoggingHandler(log_queue)
    _handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    _handler.setLevel(logging.INFO)
    _root_logger = logging.getLogger()
    _root_logger.addHandler(_handler)

    class LiveSessionManager(SessionManager):
        def __enter__(self):
            self._cat_idx = 0
            return super().__enter__()

        def run_category(self, category, extract_fn, inject_fn):
            if cancel_event.is_set():
                raise _TransferCancelled()

            total = len(self.categories)
            on_meta({
                "phase":    "extract_start",
                "category": category,
                "index":    self._cat_idx,
                "total":    total,
            })
            log_queue.put(f"-> {category}...")

            try:
                ext, inj = super().run_category(category, extract_fn, inject_fn)
                self._cat_idx += 1
                on_meta({
                    "phase":    "done",
                    "category": category,
                    "index":    self._cat_idx,
                    "total":    total,
                })
                log_queue.put(f"   done: extracted={ext}  injected={inj}")
                return ext, inj
            except _TransferCancelled:
                raise
            except Exception as exc:
                self._cat_idx += 1
                on_meta({
                    "phase":    "error",
                    "category": category,
                    "index":    self._cat_idx,
                    "total":    total,
                })
                log_queue.put(f"   error: {exc}")
                raise

    original_sm = pm_mod.SessionManager
    pm_mod.SessionManager = LiveSessionManager  # type: ignore[attr-defined]
    try:
        return pm.run()
    except _TransferCancelled:
        log_queue.put("Transfer cancelled by user.")
        return {
            "session_id": None,
            "cancelled": True,
            "source":      {"platform": pm.source.platform, "serial": pm.source.serial},
            "destination": {"platform": pm.destination.platform, "serial": pm.destination.serial},
            "categories": {},
        }
    finally:
        pm_mod.SessionManager = original_sm  # type: ignore[attr-defined]
        _root_logger.removeHandler(_handler)
