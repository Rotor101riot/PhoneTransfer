"""
ui/quirk_checklist_dialog.py

Two modal dialogs for the device-quirks feature:

QuirkChecklistDialog
    Shown BEFORE a transfer starts when compatibility issues are detected.
    Lists every applicable quirk grouped by device role with numbered steps.
    Single "OK, Start Transfer" button — no per-step acknowledgement required.
    If no quirks are detected the caller should skip showing this dialog.

RevertReminderDialog
    Shown AFTER a transfer completes for any quirks that have revert_steps
    (settings the user should restore, e.g. USB Restricted Mode back ON).
    Simple "Got it" button.

Usage
-----
    from ui.quirk_checklist_dialog import QuirkChecklistDialog, RevertReminderDialog
    from core.quirk_detector import detect_quirks

    pairs = detect_quirks(source_dev, dest_dev)

    # Before transfer (only show if pairs is non-empty):
    if pairs:
        dlg = QuirkChecklistDialog(parent, pairs=pairs,
                                   source_label="iPhone 14",
                                   dest_label="Galaxy S23")
        if not dlg.result:
            return  # user clicked Cancel

    # After transfer:
    revert = [(q, r) for q, r in pairs if q.revert_steps]
    if revert:
        RevertReminderDialog(parent, pairs=revert,
                             source_label="iPhone 14",
                             dest_label="Galaxy S23")
"""

from __future__ import annotations

import customtkinter as ctk

from core.quirk_detector import Quirk

# ---------------------------------------------------------------------------
# Shared palette (matches the app's dark-blue theme)
# ---------------------------------------------------------------------------
_WARN_COLOR = "#E8A838"   # amber — warnings
_INFO_COLOR = "#4CA3E0"   # blue  — informational
_OK_COLOR   = "#3BA55D"   # green — proceed button
_STEP_FG    = ("gray25", "gray82")
_CARD_FG    = ("gray92", "gray18")
_PAD        = 12


# ---------------------------------------------------------------------------
# Shared helper — build one quirk card inside a parent frame
# ---------------------------------------------------------------------------

def _build_quirk_card(parent: ctk.CTkFrame, quirk: Quirk) -> None:
    """Render a single quirk card (title + description + numbered steps)."""
    card = ctk.CTkFrame(parent, corner_radius=8, fg_color=_CARD_FG)
    card.pack(fill="x", padx=0, pady=(0, 10))
    card.grid_columnconfigure(1, weight=1)

    badge_color = _WARN_COLOR if quirk.severity == "warning" else _INFO_COLOR
    badge_text  = "!" if quirk.severity == "warning" else "i"

    ctk.CTkLabel(
        card,
        text=badge_text,
        font=ctk.CTkFont(size=15, weight="bold"),
        text_color=badge_color,
        width=28,
    ).grid(row=0, column=0, padx=(_PAD, 4), pady=(_PAD, 0), sticky="n")

    ctk.CTkLabel(
        card,
        text=quirk.title,
        font=ctk.CTkFont(size=13, weight="bold"),
        anchor="w",
        wraplength=460,
    ).grid(row=0, column=1, padx=(0, _PAD), pady=(_PAD, 2), sticky="ew")

    ctk.CTkLabel(
        card,
        text=quirk.description,
        font=ctk.CTkFont(size=12),
        text_color=("gray35", "gray68"),
        anchor="w",
        justify="left",
        wraplength=460,
    ).grid(row=1, column=0, columnspan=2, padx=_PAD, pady=(0, 6), sticky="ew")

    if quirk.steps:
        steps_frame = ctk.CTkFrame(card, fg_color="transparent")
        steps_frame.grid(row=2, column=0, columnspan=2,
                         padx=_PAD, pady=(0, _PAD), sticky="ew")
        steps_frame.grid_columnconfigure(1, weight=1)

        for i, step_text in enumerate(quirk.steps, start=1):
            ctk.CTkLabel(
                steps_frame,
                text=f"{i}.",
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=badge_color,
                width=22,
                anchor="ne",
            ).grid(row=i - 1, column=0, padx=(0, 4), pady=2, sticky="ne")

            ctk.CTkLabel(
                steps_frame,
                text=step_text,
                font=ctk.CTkFont(size=12),
                text_color=_STEP_FG,
                anchor="w",
                justify="left",
                wraplength=430,
            ).grid(row=i - 1, column=1, pady=2, sticky="ew")
    else:
        ctk.CTkFrame(card, fg_color="transparent", height=_PAD // 2).grid(
            row=2, column=0, columnspan=2
        )


# ---------------------------------------------------------------------------
# Pre-transfer dialog
# ---------------------------------------------------------------------------

class QuirkChecklistDialog(ctk.CTkToplevel):
    """
    Modal pre-transfer dialog displaying device compatibility notes.

    Parameters
    ----------
    parent       : The parent Tk window.
    pairs        : list of (Quirk, role) from detect_quirks().
    source_label : Human-readable name for the source device.
    dest_label   : Human-readable name for the destination device.

    Attributes
    ----------
    result : bool
        True  — user clicked "OK, Start Transfer".
        False — user clicked "Cancel" or closed the window.
    """

    def __init__(
        self,
        parent: ctk.CTk,
        pairs: list[tuple[Quirk, str]],
        source_label: str = "Source",
        dest_label:   str = "Destination",
    ) -> None:
        super().__init__(parent)
        self.title("Before You Transfer")
        self.resizable(False, True)
        self.grab_set()

        self.result: bool = False

        n = len(pairs)
        h = min(800, 280 + n * 155)
        self.geometry(f"580x{h}")
        self.minsize(560, 260)

        self._build_ui(pairs, source_label, dest_label)

        self.update_idletasks()
        px = parent.winfo_rootx() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.wait_window()

    def _build_ui(
        self,
        pairs: list[tuple[Quirk, str]],
        source_label: str,
        dest_label: str,
    ) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Header
        header = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray88", "gray16"))
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text="Before You Transfer",
            font=ctk.CTkFont(size=17, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, padx=_PAD, pady=(_PAD, 2), sticky="w")

        warning_count = sum(1 for q, _ in pairs if q.severity == "warning")
        info_count    = sum(1 for q, _ in pairs if q.severity == "info")
        sub = []
        if warning_count:
            sub.append(f"{warning_count} item{'s' if warning_count > 1 else ''} need your attention")
        if info_count:
            sub.append(f"{info_count} tip{'s' if info_count > 1 else ''} to be aware of")
        subtitle = "  •  ".join(sub) if sub else "Review the notes below."

        ctk.CTkLabel(
            header,
            text=subtitle,
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65"),
            anchor="w",
        ).grid(row=1, column=0, padx=_PAD, pady=(0, _PAD), sticky="w")

        # Scrollable body
        scroll = ctk.CTkScrollableFrame(self, corner_radius=0, fg_color="transparent")
        scroll.grid(row=1, column=0, sticky="nsew", padx=_PAD, pady=_PAD)
        scroll.grid_columnconfigure(0, weight=1)

        source_pairs = [(q, r) for q, r in pairs if r == "source"]
        dest_pairs   = [(q, r) for q, r in pairs if r == "destination"]

        for group_label, group_pairs in [
            (f"SOURCE  —  {source_label}", source_pairs),
            (f"DESTINATION  —  {dest_label}", dest_pairs),
        ]:
            if not group_pairs:
                continue
            ctk.CTkLabel(
                scroll,
                text=group_label,
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color=("gray45", "gray60"),
                anchor="w",
            ).pack(fill="x", padx=4, pady=(8, 4))

            for quirk, _ in group_pairs:
                _build_quirk_card(scroll, quirk)

        # Footer
        footer = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray88", "gray16"))
        footer.grid(row=2, column=0, sticky="ew")

        btn_row = ctk.CTkFrame(footer, fg_color="transparent")
        btn_row.pack(side="right", padx=_PAD, pady=_PAD)

        ctk.CTkButton(
            btn_row,
            text="Cancel",
            width=100,
            fg_color=("gray75", "gray30"),
            hover_color=("gray65", "gray40"),
            text_color=("gray20", "gray90"),
            command=self._on_cancel,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_row,
            text="OK, Start Transfer",
            width=170,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=_OK_COLOR,
            hover_color="#2E8B47",
            command=self._on_ok,
        ).pack(side="left")

    def _on_ok(self) -> None:
        self.result = True
        self.grab_release()
        self.destroy()

    def _on_cancel(self) -> None:
        self.result = False
        self.grab_release()
        self.destroy()


# ---------------------------------------------------------------------------
# Post-transfer revert reminder
# ---------------------------------------------------------------------------

class RevertReminderDialog(ctk.CTkToplevel):
    """
    Modal post-transfer dialog reminding the user to restore any settings
    they changed before the transfer (e.g. USB Restricted Mode, PTP mode).

    Only shown when at least one active quirk has non-empty revert_steps.
    """

    def __init__(
        self,
        parent: ctk.CTk,
        pairs: list[tuple[Quirk, str]],
        source_label: str = "Source",
        dest_label:   str = "Destination",
    ) -> None:
        super().__init__(parent)
        self.title("Settings to Restore")
        self.resizable(False, True)
        self.grab_set()

        n = len(pairs)
        h = min(640, 240 + n * 120)
        self.geometry(f"540x{h}")
        self.minsize(520, 200)

        self._build_ui(pairs, source_label, dest_label)

        self.update_idletasks()
        px = parent.winfo_rootx() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.wait_window()

    def _build_ui(
        self,
        pairs: list[tuple[Quirk, str]],
        source_label: str,
        dest_label: str,
    ) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Header
        header = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray88", "gray16"))
        header.grid(row=0, column=0, sticky="ew")

        ctk.CTkLabel(
            header,
            text="Transfer Complete — Settings to Restore",
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, padx=_PAD, pady=(_PAD, 2), sticky="w")

        ctk.CTkLabel(
            header,
            text="You may have changed some settings to enable the transfer. Here's what to put back.",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray65"),
            anchor="w",
            wraplength=500,
        ).grid(row=1, column=0, padx=_PAD, pady=(0, _PAD), sticky="w")

        # Scrollable body
        scroll = ctk.CTkScrollableFrame(self, corner_radius=0, fg_color="transparent")
        scroll.grid(row=1, column=0, sticky="nsew", padx=_PAD, pady=_PAD)
        scroll.grid_columnconfigure(0, weight=1)

        source_pairs = [(q, r) for q, r in pairs if r == "source"]
        dest_pairs   = [(q, r) for q, r in pairs if r == "destination"]

        for group_label, group_pairs in [
            (f"SOURCE  —  {source_label}", source_pairs),
            (f"DESTINATION  —  {dest_label}", dest_pairs),
        ]:
            if not group_pairs:
                continue

            ctk.CTkLabel(
                scroll,
                text=group_label,
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color=("gray45", "gray60"),
                anchor="w",
            ).pack(fill="x", padx=4, pady=(8, 4))

            for quirk, _ in group_pairs:
                card = ctk.CTkFrame(scroll, corner_radius=8, fg_color=_CARD_FG)
                card.pack(fill="x", padx=0, pady=(0, 8))
                card.grid_columnconfigure(1, weight=1)

                ctk.CTkLabel(
                    card,
                    text="↩",
                    font=ctk.CTkFont(size=15),
                    text_color=_INFO_COLOR,
                    width=28,
                ).grid(row=0, column=0, padx=(_PAD, 4), pady=(_PAD, 0), sticky="n")

                ctk.CTkLabel(
                    card,
                    text=quirk.title,
                    font=ctk.CTkFont(size=13, weight="bold"),
                    anchor="w",
                    wraplength=420,
                ).grid(row=0, column=1, padx=(0, _PAD), pady=(_PAD, 4), sticky="ew")

                steps_frame = ctk.CTkFrame(card, fg_color="transparent")
                steps_frame.grid(row=1, column=0, columnspan=2,
                                 padx=_PAD, pady=(0, _PAD), sticky="ew")
                steps_frame.grid_columnconfigure(1, weight=1)

                for i, step_text in enumerate(quirk.revert_steps, start=1):
                    ctk.CTkLabel(
                        steps_frame,
                        text=f"{i}.",
                        font=ctk.CTkFont(size=12, weight="bold"),
                        text_color=_INFO_COLOR,
                        width=22,
                        anchor="ne",
                    ).grid(row=i - 1, column=0, padx=(0, 4), pady=2, sticky="ne")

                    ctk.CTkLabel(
                        steps_frame,
                        text=step_text,
                        font=ctk.CTkFont(size=12),
                        text_color=_STEP_FG,
                        anchor="w",
                        justify="left",
                        wraplength=395,
                    ).grid(row=i - 1, column=1, pady=2, sticky="ew")

        # Footer
        footer = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray88", "gray16"))
        footer.grid(row=2, column=0, sticky="ew")

        ctk.CTkButton(
            footer,
            text="Got it",
            width=110,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=_OK_COLOR,
            hover_color="#2E8B47",
            command=self._on_close,
        ).pack(side="right", padx=_PAD, pady=_PAD)

    def _on_close(self) -> None:
        self.grab_release()
        self.destroy()
