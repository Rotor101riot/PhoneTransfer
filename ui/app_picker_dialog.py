"""
ui/app_picker_dialog.py

Scrollable app-selection dialog shown before a transfer when the 'apps'
category is checked.

Features
--------
- Populated by extract_apps_android.list_packages() in a background thread
  so the dialog opens immediately without blocking the UI.
- Real-time search filter on package name.
- Per-row checkbox with package name, display version, and APK size in MB.
- Select All / Deselect All buttons.
- Selected-count label updates live.
- OK returns the list of selected package names.
- Cancel / window-close returns an empty list.

Usage
-----
    from ui.app_picker_dialog import AppPickerDialog

    dialog = AppPickerDialog(parent, serial="BP98109AA13C2001861")
    selected = dialog.result   # list[str] of package names, or [] on cancel
"""

from __future__ import annotations

import logging
import threading

import customtkinter as ctk

logger = logging.getLogger(__name__)


class AppPickerDialog(ctk.CTkToplevel):
    """
    Modal dialog for selecting which apps to transfer.

    Parameters
    ----------
    parent:
        Parent CTk window.
    serial:
        ADB serial of the *source* device.  Used to call list_packages().
    """

    def __init__(self, parent: ctk.CTk, serial: str) -> None:
        super().__init__(parent)
        self.title("Select Apps to Transfer")
        self.geometry("520x560")
        self.minsize(400, 400)
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        self._serial = serial
        self.result: list[str] = []          # filled on OK

        # Internal state
        self._all_packages: dict[str, dict] = {}   # pkg -> {version_name, apk_size_mb}
        self._vars: dict[str, ctk.BooleanVar] = {}
        self._rows: dict[str, ctk.CTkFrame] = {}
        self._filter: str = ""
        self._loading = True

        self._build_ui()
        self._load_packages()

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.wait_window()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # Search row
        search_frame = ctk.CTkFrame(self, fg_color="transparent")
        search_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))
        search_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            search_frame, text="Search:", font=ctk.CTkFont(size=13)
        ).grid(row=0, column=0, padx=(0, 6))

        self._search_var = ctk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._apply_filter())
        ctk.CTkEntry(
            search_frame, textvariable=self._search_var,
            placeholder_text="Filter by package name…",
            font=ctk.CTkFont(size=13), height=30,
        ).grid(row=0, column=1, sticky="ew")

        # Select all / none
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 4))

        ctk.CTkButton(
            btn_row, text="All", width=60, height=26,
            command=lambda: self._select_all(True),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            btn_row, text="None", width=60, height=26,
            command=lambda: self._select_all(False),
        ).pack(side="left")

        self._count_label = ctk.CTkLabel(
            btn_row, text="Loading…", font=ctk.CTkFont(size=12),
            text_color="gray",
        )
        self._count_label.pack(side="right")

        # Scrollable list
        self._scroll = ctk.CTkScrollableFrame(self, corner_radius=6)
        self._scroll.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 8))
        self._scroll.grid_columnconfigure(0, weight=1)

        self._loading_label = ctk.CTkLabel(
            self._scroll,
            text="Loading installed apps…",
            font=ctk.CTkFont(size=13), text_color="gray",
        )
        self._loading_label.pack(pady=40)

        # OK / Cancel
        bot = ctk.CTkFrame(self, fg_color="transparent")
        bot.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))

        ctk.CTkButton(
            bot, text="Cancel", width=90,
            fg_color=("gray75", "gray35"),
            hover_color="#E05252",
            text_color=("gray15", "gray90"),
            command=self._on_cancel,
        ).pack(side="right", padx=(6, 0))

        self._ok_btn = ctk.CTkButton(
            bot, text="OK", width=90,
            command=self._on_ok,
        )
        self._ok_btn.pack(side="right")

    # ------------------------------------------------------------------
    # Package loading (background thread)
    # ------------------------------------------------------------------

    def _load_packages(self) -> None:
        def _worker():
            try:
                from core.extract_apps_android import list_packages
                pkgs = list_packages(self._serial)
            except Exception as exc:
                logger.error("AppPickerDialog: failed to list packages: %s", exc)
                pkgs = {}
            self.after(0, self._on_packages_loaded, pkgs)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_packages_loaded(self, packages: dict[str, dict]) -> None:
        self._loading = False
        self._all_packages = packages
        self._loading_label.destroy()

        # Build a checkbox row per package (sorted alphabetically)
        for pkg in sorted(packages.keys()):
            self._add_row(pkg, packages[pkg])

        self._apply_filter()
        self._update_count()

    def _add_row(self, pkg: str, info: dict) -> None:
        var = ctk.BooleanVar(value=True)
        self._vars[pkg] = var
        var.trace_add("write", lambda *_: self._update_count())

        row = ctk.CTkFrame(self._scroll, corner_radius=4, fg_color=("gray88", "gray18"))
        row.grid_columnconfigure(1, weight=1)
        self._rows[pkg] = row

        cb = ctk.CTkCheckBox(
            row, text="", variable=var, width=24,
        )
        cb.grid(row=0, column=0, rowspan=2, padx=(8, 0), pady=6)

        # Emoji + human-readable name (primary)
        emoji = info.get("emoji", "📱")
        label = info.get("label") or pkg
        ctk.CTkLabel(
            row,
            text=f"{emoji}  {label}",
            font=ctk.CTkFont(size=13),
            anchor="w",
        ).grid(row=0, column=1, sticky="ew", padx=(6, 8))

        # Package name as a smaller subtitle
        ctk.CTkLabel(
            row,
            text=pkg,
            font=ctk.CTkFont(size=10),
            text_color="gray",
            anchor="w",
        ).grid(row=1, column=1, sticky="ew", padx=(6, 8))

        size_mb = info.get("apk_size_mb", 0.0)
        vn = info.get("version_name", "")
        meta = f"{vn}  {size_mb:.1f} MB" if vn else f"{size_mb:.1f} MB"
        ctk.CTkLabel(
            row, text=meta, font=ctk.CTkFont(size=11),
            text_color="gray", anchor="e",
        ).grid(row=0, column=2, rowspan=2, padx=(0, 10))

        row.pack(fill="x", padx=2, pady=2)

    # ------------------------------------------------------------------
    # Filter / selection helpers
    # ------------------------------------------------------------------

    def _apply_filter(self) -> None:
        query = self._search_var.get().lower().strip()
        self._filter = query
        for pkg, row in self._rows.items():
            if query in pkg.lower():
                row.pack(fill="x", padx=2, pady=2)
            else:
                row.pack_forget()

    def _select_all(self, state: bool) -> None:
        for pkg, var in self._vars.items():
            if self._filter in pkg.lower():
                var.set(state)

    def _update_count(self) -> None:
        if self._loading:
            return
        n = sum(1 for v in self._vars.values() if v.get())
        total = len(self._vars)
        self._count_label.configure(text=f"{n} / {total} selected")

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def _on_ok(self) -> None:
        self.result = [pkg for pkg, var in self._vars.items() if var.get()]
        self.grab_release()
        self.destroy()

    def _on_cancel(self) -> None:
        self.result = []
        self.grab_release()
        self.destroy()
