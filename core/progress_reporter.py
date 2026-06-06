"""
progress_reporter.py

Thread-safe per-category progress tracker.
The UI layer (and test harness) can pass an on_update callback to receive
a full state snapshot after every change.
"""

import threading
from typing import Callable, Dict, Optional

CATEGORIES = [
    "contacts",
    "blocked",
    "sms",
    "photos",
    "videos",
    "calls",
    "calendar",
    "reminders",
    "notes",
    "alarms",
    "ringtones",
    "voice_memos",
    "wallpaper",
    "bookmarks",
    "whatsapp",
    "signal",
]

# Valid status values
STATUS_PENDING  = "pending"
STATUS_RUNNING  = "running"
STATUS_DONE     = "done"
STATUS_ERROR    = "error"
STATUS_SKIPPED  = "skipped"


def _blank_entry() -> dict:
    return {
        "total":     0,
        "completed": 0,
        "skipped":   0,
        "failed":    0,
        "status":    STATUS_PENDING,
    }


class ProgressReporter:
    def __init__(self, on_update: Optional[Callable[[dict], None]] = None):
        self._lock = threading.Lock()
        self._on_update = on_update
        self._state: Dict[str, dict] = {cat: _blank_entry() for cat in CATEGORIES}

    # ── Writers ───────────────────────────────────────────────────────────────

    def set_total(self, category: str, total: int) -> None:
        """Set the expected record count and mark the category as running."""
        with self._lock:
            if category not in self._state:
                self._state[category] = _blank_entry()
            self._state[category]["total"]  = total
            self._state[category]["status"] = STATUS_RUNNING
        self._notify()

    def increment(
        self,
        category: str,
        count: int = 1,
        failed: bool = False,
        skipped: bool = False,
    ) -> None:
        """Record one or more processed records."""
        with self._lock:
            if category not in self._state:
                self._state[category] = _blank_entry()
            entry = self._state[category]
            if failed:
                entry["failed"]    += count
            elif skipped:
                entry["skipped"]   += count
            else:
                entry["completed"] += count
        self._notify()

    def mark_done(self, category: str) -> None:
        self._set_status(category, STATUS_DONE)

    def mark_error(self, category: str) -> None:
        self._set_status(category, STATUS_ERROR)

    def mark_skipped(self, category: str) -> None:
        self._set_status(category, STATUS_SKIPPED)

    # ── Readers ───────────────────────────────────────────────────────────────

    def get(self, category: str) -> dict:
        with self._lock:
            return dict(self._state.get(category, _blank_entry()))

    def get_all(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._state.items()}

    def get_percentage(self, category: str) -> float:
        with self._lock:
            entry = self._state.get(category, _blank_entry())
            if entry["total"] == 0:
                return 0.0
            return round((entry["completed"] / entry["total"]) * 100, 1)

    def is_complete(self) -> bool:
        """True when every non-skipped category has status done or skipped."""
        with self._lock:
            for entry in self._state.values():
                if entry["status"] in (STATUS_PENDING, STATUS_RUNNING):
                    return False
            return True

    # ── Internal ──────────────────────────────────────────────────────────────

    def _set_status(self, category: str, status: str) -> None:
        with self._lock:
            if category not in self._state:
                self._state[category] = _blank_entry()
            self._state[category]["status"] = status
        self._notify()

    def _notify(self) -> None:
        if self._on_update:
            try:
                self._on_update(self.get_all())
            except Exception:
                pass
