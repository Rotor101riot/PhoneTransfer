"""
disconnect_handler.py

Handles device disconnection events during long transfer operations.

Provides:

* :class:`DeviceDisconnectError` — exception raised when a transfer detects a
  device has gone away.
* :class:`RetryOnDisconnect` — context manager *and* decorator that retries a
  block up to *max_retries* times whenever :class:`DeviceDisconnectError` is
  raised.
* :func:`watch_ios` — background watcher thread for iOS devices using
  pymobiledevice3.
* :func:`watch_android` — background watcher thread for Android devices using
  :class:`~core.adb_manager.ADBManager`.

Typical usage::

    from core.disconnect_handler import RetryOnDisconnect, watch_android
    from core.adb_manager import ADBManager

    adb = ADBManager()
    stop_event = watch_android(
        serial="emulator-5554",
        adb=adb,
        callback=lambda: print("Android device disconnected!"),
    )

    with RetryOnDisconnect(max_retries=3, delay_seconds=5.0):
        transfer_data()

    stop_event.set()   # stop the watcher thread when done
"""

from __future__ import annotations

import logging
import threading
import time
from functools import wraps
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from core.adb_manager import ADBManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class DeviceDisconnectError(RuntimeError):
    """
    Raised when a device is detected as disconnected during an operation.

    Transfer code should raise this instead of a generic exception so that
    :class:`RetryOnDisconnect` can catch and retry appropriately.
    """


# ---------------------------------------------------------------------------
# RetryOnDisconnect — context manager and decorator
# ---------------------------------------------------------------------------


class RetryOnDisconnect:
    """
    Retry a code block or function when :class:`DeviceDisconnectError` is raised.

    Can be used as a **context manager**::

        with RetryOnDisconnect(max_retries=3, delay_seconds=5.0):
            do_transfer()

    Or as a **decorator**::

        @RetryOnDisconnect(max_retries=3, delay_seconds=5.0)
        def do_transfer():
            ...

    Parameters
    ----------
    max_retries:
        Maximum number of retry attempts after the first failure.  A value of
        ``3`` means up to 4 total attempts.
    delay_seconds:
        Seconds to wait between attempts.
    logger:
        Optional logger instance.  If ``None``, the module-level logger is used.
    """

    def __init__(
        self,
        max_retries: int = 3,
        delay_seconds: float = 5.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._max_retries = max_retries
        self._delay = delay_seconds
        self._log = logger or globals()["logger"]

        # Context manager state
        self._attempt: int = 0
        self._last_error: DeviceDisconnectError | None = None

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "RetryOnDisconnect":
        self._attempt = 0
        self._last_error = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is None:
            return False  # no exception — normal exit

        if not issubclass(exc_type, DeviceDisconnectError):
            return False  # some other exception — propagate it

        self._last_error = exc_val  # type: ignore[assignment]
        self._attempt += 1

        if self._attempt <= self._max_retries:
            self._log.warning(
                "DeviceDisconnectError on attempt %d/%d: %s. Retrying in %.1fs …",
                self._attempt,
                self._max_retries + 1,
                exc_val,
                self._delay,
            )
            time.sleep(self._delay)
            # Signal that the exception was handled; caller must re-enter the
            # context manager for the retry.  When used as a decorator (below)
            # the retry loop is inside __call__ itself.
            return True  # suppress exception so the caller can re-enter

        self._log.error(
            "DeviceDisconnectError: exceeded max_retries=%d. Last error: %s",
            self._max_retries,
            exc_val,
        )
        return False  # re-raise after exhausting retries

    # ------------------------------------------------------------------
    # Decorator protocol
    # ------------------------------------------------------------------

    def __call__(self, func: Callable) -> Callable:
        """
        Wrap *func* with retry logic.

        Each time :class:`DeviceDisconnectError` is raised, the function is
        retried after *delay_seconds* up to *max_retries* times.
        """
        max_retries = self._max_retries
        delay = self._delay
        log = self._log

        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except DeviceDisconnectError as exc:
                    if attempt < max_retries:
                        log.warning(
                            "DeviceDisconnectError in %s (attempt %d/%d): %s. "
                            "Retrying in %.1fs …",
                            func.__name__,
                            attempt + 1,
                            max_retries + 1,
                            exc,
                            delay,
                        )
                        time.sleep(delay)
                    else:
                        log.error(
                            "DeviceDisconnectError in %s: exceeded max_retries=%d. "
                            "Last error: %s",
                            func.__name__,
                            max_retries,
                            exc,
                        )
                        raise

        return wrapper


# ---------------------------------------------------------------------------
# iOS device watcher
# ---------------------------------------------------------------------------


def watch_ios(
    udid: str,
    callback: Callable[[], None],
    poll_interval: float = 2.0,
) -> threading.Event:
    """
    Start a background thread that monitors an iOS device for disconnection.

    Polls :class:`pymobiledevice3.lockdown.LockdownClient` every
    *poll_interval* seconds.  When the device is no longer reachable the
    *callback* is invoked exactly once and the returned :class:`threading.Event`
    is set.

    Parameters
    ----------
    udid:
        UDID of the iOS device to watch.
    callback:
        Zero-argument callable invoked when the device disconnects.
    poll_interval:
        Seconds between polls.

    Returns
    -------
    stop_event:
        A :class:`threading.Event` that the caller sets to stop the thread
        cleanly without triggering the callback.
    """
    stop_event = threading.Event()

    # Try to import pymobiledevice3; if unavailable log a warning and return
    # an already-set event so callers know watching is not active.
    try:
        from pymobiledevice3.lockdown import LockdownClient  # type: ignore[import]
    except ImportError:
        logger.warning(
            "watch_ios: pymobiledevice3 not available — iOS disconnect watching "
            "is disabled for UDID %s", udid
        )
        return stop_event

    def _poll_loop() -> None:
        disconnected = False
        while not stop_event.is_set():
            reachable = False
            lockdown = None
            try:
                lockdown = LockdownClient(serial=udid)
                reachable = True
            except Exception:  # noqa: BLE001
                reachable = False
            finally:
                if lockdown is not None:
                    try:
                        lockdown.close()
                    except Exception:
                        pass

            if not reachable and not disconnected:
                disconnected = True
                logger.warning("watch_ios: iOS device %s is no longer reachable", udid)
                try:
                    callback()
                except Exception as cb_exc:  # noqa: BLE001
                    logger.error("watch_ios: callback raised an exception: %s", cb_exc)
                stop_event.set()
                return

            stop_event.wait(timeout=poll_interval)

    thread = threading.Thread(
        target=_poll_loop,
        name=f"watch_ios-{udid[:8]}",
        daemon=True,
    )
    thread.start()
    logger.debug("watch_ios: watcher thread started for UDID %s", udid)
    return stop_event


# ---------------------------------------------------------------------------
# Android device watcher
# ---------------------------------------------------------------------------


def watch_android(
    serial: str,
    adb: "ADBManager",
    callback: Callable[[], None],
    poll_interval: float = 2.0,
) -> threading.Event:
    """
    Start a background thread that monitors an Android device for disconnection.

    Polls ``adb -s <serial> get-state`` every *poll_interval* seconds.  When
    the command fails or returns a non-zero exit code the *callback* is invoked
    exactly once.

    Parameters
    ----------
    serial:
        ADB device serial string.
    adb:
        An initialised :class:`~core.adb_manager.ADBManager` instance.
    callback:
        Zero-argument callable invoked when the device disconnects.
    poll_interval:
        Seconds between polls.

    Returns
    -------
    stop_event:
        A :class:`threading.Event` that the caller sets to stop the thread
        cleanly without triggering the callback.
    """
    stop_event = threading.Event()

    def _poll_loop() -> None:
        disconnected = False
        while not stop_event.is_set():
            try:
                _stdout, _stderr, rc = adb.run_device(serial, "get-state", timeout=5)
                reachable = rc == 0
            except Exception:  # noqa: BLE001
                reachable = False

            if not reachable and not disconnected:
                disconnected = True
                logger.warning(
                    "watch_android: Android device %s is no longer reachable", serial
                )
                try:
                    callback()
                except Exception as cb_exc:  # noqa: BLE001
                    logger.error(
                        "watch_android: callback raised an exception: %s", cb_exc
                    )
                stop_event.set()
                return

            stop_event.wait(timeout=poll_interval)

    thread = threading.Thread(
        target=_poll_loop,
        name=f"watch_android-{serial}",
        daemon=True,
    )
    thread.start()
    logger.debug("watch_android: watcher thread started for serial %s", serial)
    return stop_event
