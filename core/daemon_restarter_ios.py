"""
daemon_restarter_ios.py

Restarts iOS lockdown / AFC daemons when they stop responding mid-transfer.
Uses pymobiledevice3 under the hood.

If pymobiledevice3 is not installed a warning is logged and all operations
gracefully return ``False`` rather than raising an ImportError.

Typical usage::

    from core.daemon_restarter_ios import restart_ios_services

    if not restart_ios_services(udid):
        raise RuntimeError("iOS device never came back online")

Or for fine-grained control::

    from core.daemon_restarter_ios import DaemonRestarter

    dr = DaemonRestarter(udid)
    dr.restart_lockdownd()
    if dr.wait_for_ready(timeout_seconds=60):
        ...
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency — pymobiledevice3
# ---------------------------------------------------------------------------

try:
    from pymobiledevice3.lockdown import LockdownClient  # type: ignore[import]

    _PYMOBILEDEVICE3_AVAILABLE = True
except ImportError:
    _PYMOBILEDEVICE3_AVAILABLE = False
    logger.warning(
        "pymobiledevice3 is not installed. "
        "DaemonRestarter will not be able to restart iOS services. "
        "Install with: pip install pymobiledevice3"
    )


def _try_lockdown_connect(udid: str) -> bool:
    """
    Attempt to instantiate a LockdownClient for *udid*.

    Returns ``True`` on success, ``False`` on any failure (including missing
    pymobiledevice3).
    """
    if not _PYMOBILEDEVICE3_AVAILABLE:
        return False

    import inspect
    from core.pmd3_asyncio import pmd3_run

    client = None
    try:
        result = LockdownClient(serial=udid)
        if inspect.iscoroutine(result):
            result = pmd3_run(result)
        client = result
        # Accessing .all_values confirms the connection is actually live.
        vals = client.all_values
        if inspect.iscoroutine(vals):
            vals = pmd3_run(vals)
        logger.debug("LockdownClient connected for UDID %s", udid)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("LockdownClient connection failed for %s: %s", udid, exc)
        return False
    finally:
        if client is not None:
            try:
                close_result = client.close()
                if inspect.iscoroutine(close_result):
                    pmd3_run(close_result)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# DaemonRestarter
# ---------------------------------------------------------------------------


class DaemonRestarter:
    """
    Manages restart of iOS daemons (lockdownd, AFC) for a specific device.

    Parameters
    ----------
    udid:
        The UDID of the target iOS device.
    """

    def __init__(self, udid: str) -> None:
        self.udid = udid

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def restart_afc(self) -> bool:
        """
        Attempt to restart the AFC (Apple File Conduit) service.

        The approach is to disconnect and reconnect the LockdownClient.  A
        successful reconnect re-initialises the session with lockdownd, which
        in turn causes the AFC service to become available again.

        Returns
        -------
        ``True`` if the reconnect succeeded, ``False`` otherwise.
        """
        if not _PYMOBILEDEVICE3_AVAILABLE:
            logger.error(
                "restart_afc: pymobiledevice3 not available — cannot restart AFC "
                "on UDID %s", self.udid
            )
            return False

        logger.info("restart_afc: attempting reconnect for UDID %s", self.udid)
        success = _try_lockdown_connect(self.udid)
        if success:
            logger.info("restart_afc: AFC service restored for UDID %s", self.udid)
        else:
            logger.error("restart_afc: failed to reconnect for UDID %s", self.udid)
        return success

    def restart_lockdownd(self) -> bool:
        """
        Attempt to reconnect the LockdownClient (effectively restarting the
        lockdownd session from the host side).

        Returns
        -------
        ``True`` if the reconnect succeeded, ``False`` otherwise.
        """
        if not _PYMOBILEDEVICE3_AVAILABLE:
            logger.warning(
                "restart_lockdownd: pymobiledevice3 not available — cannot reconnect "
                "lockdownd for UDID %s", self.udid
            )
            return False

        logger.info("restart_lockdownd: reconnecting LockdownClient for UDID %s", self.udid)
        success = _try_lockdown_connect(self.udid)
        if success:
            logger.info("restart_lockdownd: lockdownd session restored for UDID %s", self.udid)
        else:
            logger.error(
                "restart_lockdownd: could not reconnect to lockdownd for UDID %s", self.udid
            )
        return success

    def wait_for_ready(self, timeout_seconds: int = 60) -> bool:
        """
        Poll the device every 2 seconds until it responds or the timeout expires.

        Parameters
        ----------
        timeout_seconds:
            Maximum number of seconds to wait before giving up.

        Returns
        -------
        ``True`` if the device responded within the timeout, ``False``
        otherwise.
        """
        if not _PYMOBILEDEVICE3_AVAILABLE:
            logger.warning(
                "wait_for_ready: pymobiledevice3 not available — returning False "
                "for UDID %s", self.udid
            )
            return False

        logger.info(
            "wait_for_ready: polling UDID %s (timeout=%ds)", self.udid, timeout_seconds
        )
        deadline = time.monotonic() + timeout_seconds
        poll_interval = 2.0

        while time.monotonic() < deadline:
            if _try_lockdown_connect(self.udid):
                logger.info("wait_for_ready: device %s is ready", self.udid)
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sleep_time = min(poll_interval, remaining)
            logger.debug(
                "wait_for_ready: device not ready yet, sleeping %.1fs (UDID=%s)",
                sleep_time, self.udid,
            )
            time.sleep(sleep_time)

        logger.error(
            "wait_for_ready: timed out after %ds waiting for UDID %s",
            timeout_seconds, self.udid,
        )
        return False


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------


def restart_ios_services(udid: str) -> bool:
    """
    High-level helper that restarts AFC and waits up to 30 seconds for the
    device to become ready.

    Parameters
    ----------
    udid:
        The UDID of the target iOS device.

    Returns
    -------
    ``True`` if the device is ready after the restart sequence, ``False``
    otherwise.
    """
    restarter = DaemonRestarter(udid)
    # Attempt AFC restart; if it fails we still proceed to wait_for_ready
    # because the device might come back on its own.
    restarter.restart_afc()
    return restarter.wait_for_ready(timeout_seconds=30)
