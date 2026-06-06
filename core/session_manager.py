"""
session_manager.py

Run-lifecycle manager for a single PhoneTransfer session.  Wraps
session_file (persistent JSON state), ProgressReporter (per-category
counters), and standard logging into one coherent context manager.

Typical usage::

    from core.normalization_schema import DeviceInfo
    from core.session_manager import SessionManager

    mgr = SessionManager(source, destination, categories=["contacts", "sms", "photos"])
    with mgr:
        extracted, injected = mgr.run_category("contacts", extract_fn, inject_fn)
        mgr.run_category("sms", extract_fn, inject_fn)
    # session is marked complete (or aborted on exception) automatically
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Callable

from core import session_file
from core.config_loader import Config, get_config
from core.normalization_schema import DeviceInfo
from core.progress_reporter import ProgressReporter

logger = logging.getLogger(__name__)

# Categories defined by the pipeline (callers may pass a subset).
ALL_CATEGORIES: list[str] = [
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
    "installed_apps",
    "apps",
    "whatsapp",
    "signal",
    "mail_accounts",
]


class SessionManager:
    """
    Coordinates a single transfer run end-to-end.

    Creates the staging directory, owns the session JSON, drives
    per-category progress reporting, and finalizes or aborts on exit.

    Parameters
    ----------
    source:
        DeviceInfo for the source device.
    destination:
        DeviceInfo for the destination device.
    categories:
        Ordered list of category names to transfer.  Defaults to
        ALL_CATEGORIES if omitted.
    config:
        Pre-built Config instance.  If None, ``get_config()`` is called
        on first use (lazy so unit tests can pass a mock without touching
        the filesystem).

    Raises
    ------
    RuntimeError
        If ``run_category`` is called outside a ``with`` block.
    """

    def __init__(
        self,
        source: DeviceInfo,
        destination: DeviceInfo,
        categories: list[str] | None = None,
        config: Config | None = None,
        existing_session_id: str | None = None,
    ) -> None:
        self.source = source
        self.destination = destination
        self.categories: list[str] = categories if categories is not None else list(ALL_CATEGORIES)
        self._config: Config | None = config
        self._existing_session_id: str | None = existing_session_id

        # Set inside __enter__
        self._staging_dir: Path | None = None
        self._session: dict | None = None
        self._progress: ProgressReporter | None = None
        self._active: bool = False

    # ------------------------------------------------------------------
    # Config (lazy)
    # ------------------------------------------------------------------

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = get_config()
        return self._config

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> SessionManager:
        """Create (or re-open) the staging directory and session file."""
        if self._existing_session_id is not None:
            self._enter_existing()
        else:
            self._enter_new()
        return self

    def _enter_new(self) -> None:
        """Create a fresh session: new staging dir + new session.json."""
        # Use a temporary path first so we can derive the real one from session_id.
        # session_file.create uses datetime-based session_ids, so we write into a
        # provisional directory, read the id, then re-establish the canonical path.
        provisional_dir = self.config.temp_dir / "_provisional"
        provisional_dir.mkdir(parents=True, exist_ok=True)

        session = session_file.create(
            staging_dir=str(provisional_dir),
            source_platform=self.source.platform,
            dest_platform=self.destination.platform,
            source_serial=self.source.serial,
            dest_serial=self.destination.serial,
            categories=self.categories,
        )

        session_id: str = session["session_id"]
        staging_dir = self.config.temp_dir / session_id
        staging_dir.mkdir(parents=True, exist_ok=True)

        # Move the session to the canonical location, reusing the same
        # session_id — avoids a second create() call that would re-generate
        # the id and could mismatch the directory name at a second boundary.
        session_file.save(str(staging_dir), session)

        # Tidy up provisional dir (best-effort)
        try:
            (provisional_dir / "session.json").unlink(missing_ok=True)
            provisional_dir.rmdir()
        except OSError:
            pass

        self._staging_dir = staging_dir
        self._session = session
        self._progress = ProgressReporter()
        self._active = True

        logger.info(
            "Session started — id=%s  src=%s(%s)  dst=%s(%s)  categories=%s",
            session_id,
            self.source.platform,
            self.source.serial,
            self.destination.platform,
            self.destination.serial,
            self.categories,
        )

    def _enter_existing(self) -> None:
        """Re-open a previous incomplete session to resume it."""
        assert self._existing_session_id is not None
        staging_dir = self.config.temp_dir / self._existing_session_id
        existing_session = session_file.load(str(staging_dir))
        if existing_session is None:
            raise ValueError(
                f"Cannot resume: session directory not found or corrupt "
                f"at {staging_dir}"
            )

        # Mark aborted=False so __exit__ doesn't re-abort on clean completion
        existing_session["aborted"] = False
        session_file.save(str(staging_dir), existing_session)

        self._staging_dir = staging_dir
        self._session = existing_session
        self._progress = ProgressReporter()
        self._active = True

        pending = session_file.pending_categories(str(staging_dir))
        logger.info(
            "Session resumed — id=%s  src=%s(%s)  dst=%s(%s)  "
            "pending_categories=%s",
            self._existing_session_id,
            self.source.platform,
            self.source.serial,
            self.destination.platform,
            self.destination.serial,
            pending,
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> bool:
        """Mark the session complete or aborted, then clean up internal state."""
        if self._staging_dir is None:
            # __enter__ never completed; nothing to finalize
            return False

        staging = str(self._staging_dir)

        if exc_type is None:
            session_file.mark_complete(staging)
            logger.info("Session completed successfully — staging=%s", staging)
        else:
            session_file.mark_aborted(staging)
            logger.error(
                "Session aborted due to %s: %s — staging=%s",
                exc_type.__name__,
                exc_val,
                staging,
            )

        # Release cached iOS connection objects (iOSbackup, IOSServiceBroker)
        try:
            from core.device_connection_cache import clear_connection_cache
            clear_connection_cache()
        except Exception:
            pass

        self._active = False
        # Do not suppress the exception
        return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_category(
        self,
        category: str,
        extract_fn: Callable[[], list],
        inject_fn: Callable[[list], int],
    ) -> tuple[int, int]:
        """
        Execute one transfer category end-to-end.

        Steps:
        1. Mark the category as *running* in the session file.
        2. Call ``extract_fn()`` — must return a list of normalized items.
        3. Update progress with the extracted count.
        4. Call ``inject_fn(items)`` — must return the number of items
           successfully injected.
        5. Compute failed count (extracted - injected).
        6. Mark the category *complete* in the session file.
        7. Mark the category *done* in the progress reporter.

        On any exception the category is marked *failed* in both the
        session file and progress reporter, then the exception is re-raised
        so the caller (or the ``with`` block's ``__exit__``) can handle it.

        Parameters
        ----------
        category:
            One of the category strings registered with this session.
        extract_fn:
            Zero-argument callable; returns a list of normalized data objects.
        inject_fn:
            Single-argument callable accepting the extracted list; returns
            the number of records successfully written to the destination.

        Returns
        -------
        tuple[int, int]
            ``(extracted_count, injected_count)``

        Raises
        ------
        RuntimeError
            If called outside a ``with`` block.
        """
        if not self._active or self._staging_dir is None:
            raise RuntimeError(
                "run_category() must be called inside a 'with SessionManager' block."
            )

        staging = str(self._staging_dir)
        cat_staging = str(self.staging_path(category))
        progress = self._progress  # always non-None when _active

        logger.info("Starting category: %s", category)
        session_file.mark_category_running(staging, category, cat_staging)
        assert progress is not None
        progress.set_total(category, 0)  # total unknown until extraction done

        try:
            # --- Extract ---
            items: list = extract_fn()
            extracted_count = len(items)
            logger.debug("Extracted %d items for category '%s'", extracted_count, category)

            # Update progress total now that we know it
            progress.set_total(category, extracted_count)

            # --- Inject ---
            injected_count: int = inject_fn(items)
            failed_count = max(0, extracted_count - injected_count)
            logger.debug(
                "Injected %d/%d items for category '%s' (%d failed)",
                injected_count,
                extracted_count,
                category,
                failed_count,
            )

            # --- Finalize ---
            progress.increment(category, injected_count)
            if failed_count:
                progress.increment(category, failed_count, failed=True)
            progress.mark_done(category)

            session_file.mark_category_complete(
                staging, category, extracted_count, injected_count, failed_count
            )
            logger.info(
                "Category '%s' complete — extracted=%d  injected=%d  failed=%d",
                category,
                extracted_count,
                injected_count,
                failed_count,
            )
            return extracted_count, injected_count

        except Exception as exc:
            error_detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            logger.error("Category '%s' failed: %s", category, error_detail)
            progress.mark_error(category)
            session_file.mark_category_failed(staging, category, error_detail)
            raise

    def can_resume(self) -> bool:
        """
        Return True if a prior, incomplete session exists in the staging
        directory.  Uses the session_file resumability check.

        Note: this method is useful *before* entering the context manager
        (i.e., before ``__enter__`` creates a new session).  After entering,
        the current session is always in progress, so the result is False.
        """
        if self._staging_dir is not None:
            return session_file.is_resumable(str(self._staging_dir))
        # Without an established staging dir we cannot know; return False.
        return False

    @property
    def staging_dir(self) -> Path:
        """
        The root staging directory for this session.

        Raises
        ------
        RuntimeError
            If called before the context manager has been entered.
        """
        if self._staging_dir is None:
            raise RuntimeError(
                "staging_dir accessed before __enter__; enter the 'with' block first."
            )
        return self._staging_dir

    def staging_path(self, category: str) -> Path:
        """
        Return the per-category subdirectory inside the staging directory.

        The directory is *not* created here; extractors are responsible
        for creating it when they need to write files.

        Raises
        ------
        RuntimeError
            If called before the context manager has been entered.
        """
        if self._staging_dir is None:
            raise RuntimeError(
                "staging_path() called before __enter__; enter the 'with' block first."
            )
        return self._staging_dir / category

    # ------------------------------------------------------------------
    # Convenience read-only properties
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        """The session_id string from the session file, or None if not started."""
        if self._session is None:
            return None
        return self._session.get("session_id")

    @property
    def session(self) -> dict | None:
        """The raw session dict (read-only reference; mutate at your own risk)."""
        return self._session

    @property
    def progress(self) -> ProgressReporter | None:
        """The ProgressReporter for this session, or None if not started."""
        return self._progress

    def __repr__(self) -> str:
        return (
            f"SessionManager("
            f"src={self.source.platform}:{self.source.serial}, "
            f"dst={self.destination.platform}:{self.destination.serial}, "
            f"categories={self.categories}, "
            f"active={self._active})"
        )
