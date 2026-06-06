"""
ui/file_filter_dialog.py

File-type filter dialog shown when the user clicks "⚙ Media Filter" in the
category panel before an Android photos / storage transfer.

Features
--------
- Extension checkboxes grouped by media type (Images, Video, Audio, Documents, Other).
- "All" / "None" toggle per group via group-header buttons.
- Live count label showing how many extensions are enabled.
- Defaults match the built-in extractor behaviour (Images + Video checked).
- OK  → sets result to the list of enabled extensions and applies to config.
- Cancel / close → leaves config unchanged (result is None).

Usage
-----
    from ui.file_filter_dialog import FileFilterDialog
    from core.config_loader import get_config

    dlg = FileFilterDialog(parent, current=get_config().storage_filter_extensions)
    if dlg.result is not None:
        get_config().storage_filter_extensions = dlg.result
"""

from __future__ import annotations

import logging

import customtkinter as ctk

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Extension groups
# ---------------------------------------------------------------------------

# Each entry: (group_label, default_enabled, [extensions])
_GROUPS: list[tuple[str, bool, list[str]]] = [
    ("🖼  Images", True, [
        ".jpg", ".jpeg", ".png", ".heic", ".heif",
        ".gif", ".webp", ".bmp", ".tiff",
    ]),
    ("🎬  Video", True, [
        ".mp4", ".mov", ".3gp", ".mkv", ".avi", ".m4v", ".wmv", ".flv",
    ]),
    ("🎵  Audio", False, [
        ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wav", ".opus", ".wma",
    ]),
    ("📄  Documents", False, [
        ".pdf", ".docx", ".doc", ".xlsx", ".xls",
        ".pptx", ".txt", ".csv", ".epub",
    ]),
    ("📦  Other", False, [
        ".apk", ".zip", ".rar", ".7z", ".tar", ".gz",
    ]),
]


class FileFilterDialog(ctk.CTkToplevel):
    """
    Modal dialog for selecting which file types to include in a
    photos / storage transfer.

    Parameters
    ----------
    parent:
        Parent CTk window.
    current:
        The currently active extension filter (a list of strings like
        ``['.jpg', '.mp4']``), or ``None`` to use the built-in defaults.
    """

    def __init__(
        self,
        parent: ctk.CTk,
        current: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.title("Media File Type Filter")
        self.geometry("420x540")
        self.minsize(360, 400)
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        # result is set on OK; remains None if cancelled
        self.result: list[str] | None = None

        # Build the set of currently-enabled extensions so the dialog opens
        # reflecting the last saved state.
        if current is not None:
            self._active: set[str] = set(current)
        else:
            # Default: Images + Video (matching the extractor built-in set)
            self._active = set()
            for _, default_on, exts in _GROUPS:
                if default_on:
                    self._active.update(exts)

        # Maps extension string → BooleanVar
        self._vars: dict[str, ctk.BooleanVar] = {}

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.wait_window()

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 4))
        ctk.CTkLabel(
            header,
            text="Select which file types to include in the transfer.",
            font=ctk.CTkFont(size=13),
            wraplength=380,
            justify="left",
            anchor="w",
        ).pack(fill="x")

        # Scrollable group list
        scroll = ctk.CTkScrollableFrame(self, corner_radius=6)
        scroll.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 8))
        scroll.grid_columnconfigure(0, weight=1)

        for group_label, _default_on, exts in _GROUPS:
            self._add_group(scroll, group_label, exts)

        # Bottom bar: count + OK / Cancel
        bot = ctk.CTkFrame(self, fg_color="transparent")
        bot.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 14))
        bot.grid_columnconfigure(0, weight=1)

        self._count_label = ctk.CTkLabel(
            bot, text="", font=ctk.CTkFont(size=12), text_color="gray",
        )
        self._count_label.grid(row=0, column=0, sticky="w")
        self._refresh_count()

        btn_frame = ctk.CTkFrame(bot, fg_color="transparent")
        btn_frame.grid(row=0, column=1, sticky="e")
        ctk.CTkButton(
            btn_frame, text="Cancel", width=90,
            fg_color="#555", hover_color="#E05252",
            command=self._on_cancel,
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            btn_frame, text="OK", width=90,
            command=self._on_ok,
        ).pack(side="right")

    def _add_group(
        self,
        parent: ctk.CTkScrollableFrame,
        group_label: str,
        exts: list[str],
    ) -> None:
        """Add one extension group with a header and individual checkboxes."""
        # Group header row with All / None mini-buttons
        hdr = ctk.CTkFrame(parent, fg_color="transparent")
        hdr.pack(fill="x", padx=4, pady=(10, 2))
        hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            hdr,
            text=group_label,
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")

        ctk.CTkButton(
            hdr, text="All", width=38, height=20,
            font=ctk.CTkFont(size=11),
            command=lambda e=exts: self._set_group(e, True),
        ).grid(row=0, column=1, padx=(4, 2))
        ctk.CTkButton(
            hdr, text="None", width=44, height=20,
            font=ctk.CTkFont(size=11),
            command=lambda e=exts: self._set_group(e, False),
        ).grid(row=0, column=2)

        # Checkboxes in a 3-column flow
        grid = ctk.CTkFrame(parent, fg_color="transparent")
        grid.pack(fill="x", padx=4)

        for col_idx in range(3):
            grid.grid_columnconfigure(col_idx, weight=1)

        for idx, ext in enumerate(exts):
            var = ctk.BooleanVar(value=(ext in self._active))
            self._vars[ext] = var
            var.trace_add("write", lambda *_: self._refresh_count())

            ctk.CTkCheckBox(
                grid,
                text=ext,
                variable=var,
                font=ctk.CTkFont(size=12),
                width=20,
            ).grid(row=idx // 3, column=idx % 3, sticky="w", padx=4, pady=2)

        # Thin separator
        ctk.CTkFrame(parent, height=1, fg_color=("gray75", "gray30")).pack(
            fill="x", padx=4, pady=(6, 0)
        )

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _set_group(self, exts: list[str], state: bool) -> None:
        for ext in exts:
            if ext in self._vars:
                self._vars[ext].set(state)

    def _refresh_count(self) -> None:
        n = sum(1 for v in self._vars.values() if v.get())
        total = len(self._vars)
        self._count_label.configure(text=f"{n} of {total} types selected")

    # -----------------------------------------------------------------------
    # Button handlers
    # -----------------------------------------------------------------------

    def _on_ok(self) -> None:
        self.result = [ext for ext, var in self._vars.items() if var.get()]
        if not self.result:
            # If nothing is selected treat it as "no filter" to avoid pulling
            # zero files, which would surprise the user.
            self.result = None
        self.grab_release()
        self.destroy()

    def _on_cancel(self) -> None:
        self.result = None
        self.grab_release()
        self.destroy()
