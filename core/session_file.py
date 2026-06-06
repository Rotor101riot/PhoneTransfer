"""
session_file.py

Reads and writes a session.json file inside the staging directory.
Provides resumable transfer state — which categories finished, counts,
and errors. All functions operate on the same file path convention so
any module can call them without passing state around.
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Write cache — avoids a full disk read/write on every record_file_progress()
# call during chunk streaming.  Only record_file_progress() uses this cache;
# all structural writes (mark_category_*, mark_complete, etc.) bypass it and
# call _invalidate_cache() so the next streaming call reloads fresh state.
# ---------------------------------------------------------------------------
_session_cache: dict[str, dict] = {}
_cache_flush_time: dict[str, float] = {}
_FLUSH_INTERVAL_S: float = 5.0


def _invalidate_cache(staging_dir: str) -> None:
    """Discard the in-memory session cache for *staging_dir*."""
    _session_cache.pop(staging_dir, None)
    _cache_flush_time.pop(staging_dir, None)

SESSION_FILENAME = "session.json"


def _path(staging_dir: str) -> Path:
    return Path(staging_dir) / SESSION_FILENAME


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def create(
    staging_dir: str,
    source_platform: str,
    dest_platform: str,
    source_serial: str,
    dest_serial: str,
    categories: list[str],
) -> dict:
    """
    Create a fresh session file. Overwrites any existing session.
    Returns the session dict.
    """
    now = datetime.now().isoformat()
    session = {
        "session_id":      datetime.now().strftime("%Y%m%d_%H%M%S"),
        "created_at":      now,
        "updated_at":      now,
        "source_platform": source_platform,   # "ios" | "android"
        "dest_platform":   dest_platform,     # "ios" | "android"
        "source_serial":   source_serial,
        "dest_serial":     dest_serial,
        "categories": {
            cat: {
                "status":          "pending",  # pending|running|completed|failed
                "extracted_count": 0,
                "injected_count":  0,
                "failed_count":    0,
                "staging_path":    None,
                "error":           None,
            }
            for cat in categories
        },
        "completed": False,
        "aborted":   False,
    }
    _write(staging_dir, session)
    return session


def load(staging_dir: str) -> Optional[dict]:
    """Return the session dict, or None if the file is missing or corrupt."""
    p = _path(staging_dir)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def save(staging_dir: str, session: dict) -> None:
    """Persist a (possibly modified) session dict back to disk."""
    _write(staging_dir, session)


# ── Category updates ──────────────────────────────────────────────────────────

def mark_category_running(staging_dir: str, category: str, staging_path: str) -> None:
    session = load(staging_dir)
    if not session or category not in session["categories"]:
        return
    session["categories"][category].update({
        "status":       "running",
        "staging_path": staging_path,
    })
    _write(staging_dir, session)
    _invalidate_cache(staging_dir)


def mark_category_complete(
    staging_dir: str,
    category: str,
    extracted: int,
    injected: int,
    failed: int,
) -> None:
    session = load(staging_dir)
    if not session or category not in session["categories"]:
        return
    session["categories"][category].update({
        "status":          "completed",
        "extracted_count": extracted,
        "injected_count":  injected,
        "failed_count":    failed,
    })
    _write(staging_dir, session)
    _invalidate_cache(staging_dir)


def mark_category_failed(staging_dir: str, category: str, error: str) -> None:
    session = load(staging_dir)
    if not session or category not in session["categories"]:
        return
    session["categories"][category].update({
        "status": "failed",
        "error":  error,
    })
    _write(staging_dir, session)
    _invalidate_cache(staging_dir)


# ── Per-file transfer tracking (resumable streaming) ────────────────────────

def record_file_progress(
    staging_dir: str,
    category: str,
    filename: str,
    bytes_transferred: int,
    total_bytes: int,
    md5_partial: str = "",
) -> None:
    """
    Record transfer progress for a single file within a category.

    This enables resumable transfers — if the session is interrupted, the
    pipeline can query ``get_file_progress()`` to determine where to resume.

    Uses an in-memory write cache so that rapid per-chunk calls don't issue
    a full disk read/write cycle for every 4 MB chunk.  State is flushed to
    disk at most every ``_FLUSH_INTERVAL_S`` seconds, and always immediately
    when the file transfer completes.
    """
    # Prefer cached session over a fresh disk read
    session = _session_cache.get(staging_dir) or load(staging_dir)
    if not session:
        return

    complete = bytes_transferred >= total_bytes > 0
    files = session.setdefault("file_progress", {}).setdefault(category, {})
    files[filename] = {
        "bytes_transferred": bytes_transferred,
        "total_bytes":       total_bytes,
        "md5_partial":       md5_partial,
        "complete":          complete,
    }
    _session_cache[staging_dir] = session

    now = time.monotonic()
    if complete or now - _cache_flush_time.get(staging_dir, 0.0) >= _FLUSH_INTERVAL_S:
        _write(staging_dir, session)
        _cache_flush_time[staging_dir] = now


def get_file_progress(
    staging_dir: str, category: str, filename: str,
) -> Optional[dict]:
    """
    Return the transfer state for a single file, or None if not tracked.

    The returned dict has:
    ``bytes_transferred``, ``total_bytes``, ``md5_partial``, ``complete``.
    """
    session = load(staging_dir)
    if not session:
        return None
    return (
        session.get("file_progress", {})
        .get(category, {})
        .get(filename)
    )


def pending_files(staging_dir: str, category: str) -> list[str]:
    """
    Return filenames in *category* that were started but not completed.

    These are candidates for resumable re-transfer.
    """
    session = load(staging_dir)
    if not session:
        return []
    cat_files = session.get("file_progress", {}).get(category, {})
    return [
        name for name, state in cat_files.items()
        if not state.get("complete", False)
    ]


def clear_file_progress(staging_dir: str, category: str) -> None:
    """Remove all per-file progress entries for a category."""
    session = load(staging_dir)
    if not session:
        return
    fp = session.get("file_progress", {})
    if category in fp:
        del fp[category]
        _write(staging_dir, session)
        _invalidate_cache(staging_dir)


# ── Session completion ────────────────────────────────────────────────────────

def mark_complete(staging_dir: str) -> None:
    session = load(staging_dir)
    if not session:
        return
    session["completed"] = True
    _write(staging_dir, session)
    _invalidate_cache(staging_dir)


def mark_aborted(staging_dir: str) -> None:
    session = load(staging_dir)
    if not session:
        return
    session["aborted"] = True
    _write(staging_dir, session)
    _invalidate_cache(staging_dir)


# ── Queries ───────────────────────────────────────────────────────────────────

def exists(staging_dir: str) -> bool:
    return _path(staging_dir).exists()


def is_resumable(staging_dir: str) -> bool:
    session = load(staging_dir)
    if not session:
        return False
    return not session.get("completed", False) and not session.get("aborted", False)


def pending_categories(staging_dir: str) -> list[str]:
    """Return category names that have not yet completed."""
    session = load(staging_dir)
    if not session:
        return []
    return [
        cat for cat, state in session["categories"].items()
        if state["status"] in ("pending", "running", "failed")
    ]


# ── Internal ──────────────────────────────────────────────────────────────────

def _write(staging_dir: str, session: dict) -> None:
    p = _path(staging_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    session["updated_at"] = datetime.now().isoformat()
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(session, fh, separators=(',', ':'))
