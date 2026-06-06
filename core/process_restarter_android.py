"""
process_restarter_android.py

Restarts the PhoneTransfer companion APK on the Android device if it crashes,
and re-establishes the ADB TCP forward tunnel afterward.

Constants
---------
COMPANION_PACKAGE : str
    Android package name of the companion APK.
COMPANION_PORT : int
    Device-side TCP port the APK listens on (mirrored by ADB forward).

Typical usage::

    from core.adb_manager import ADBManager
    from core.process_restarter_android import restart_companion

    adb = ADBManager()
    if not restart_companion(serial="emulator-5554", adb=adb):
        raise RuntimeError("Companion APK failed to restart")
"""

from __future__ import annotations

import logging
import socket as _socket_mod
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.adb_manager import ADBManager

logger = logging.getLogger(__name__)

COMPANION_PACKAGE: str = "com.phonetransfer.companion"
COMPANION_PORT: int = 7337


class ProcessRestarter:
    """
    Manages the lifecycle of the PhoneTransfer companion APK on an Android device.

    Parameters
    ----------
    serial:
        ADB device serial string.
    adb:
        An initialised :class:`~core.adb_manager.ADBManager` instance.
    """

    def __init__(self, serial: str, adb: "ADBManager") -> None:
        self._serial = serial
        self._adb = adb

    # ------------------------------------------------------------------
    # State inspection
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        """
        Check whether the companion APK process is currently running.

        Uses ``pidof`` on the device; a non-empty stdout indicates at least
        one PID was found.

        Returns
        -------
        ``True`` if the process appears to be running.
        """
        stdout, _stderr, rc = self._adb.shell(
            self._serial,
            f"pidof {COMPANION_PACKAGE} || echo ''",
        )
        if rc != 0:
            logger.debug(
                "is_running: shell command returned rc=%d for %s", rc, self._serial
            )
            return False
        running = bool(stdout.strip())
        logger.debug(
            "is_running: %s on %s → %s (stdout=%r)",
            COMPANION_PACKAGE, self._serial, running, stdout.strip(),
        )
        return running

    # ------------------------------------------------------------------
    # Lifecycle control
    # ------------------------------------------------------------------

    def force_stop(self) -> None:
        """
        Force-stop the companion APK using the Android activity manager.

        Equivalent to: ``adb shell am force-stop <package>``
        """
        logger.info(
            "force_stop: stopping %s on %s", COMPANION_PACKAGE, self._serial
        )
        self._adb.shell(
            self._serial,
            f"am force-stop {COMPANION_PACKAGE}",
        )

    def start_service(self) -> bool:
        """
        Start the companion APK's background service via the activity manager.

        Equivalent to: ``adb shell am startservice <package>/.TransferService``

        Returns
        -------
        ``True`` if the command returned exit code 0.
        """
        _stdout, stderr, rc = self._adb.shell(
            self._serial,
            f"am startservice {COMPANION_PACKAGE}/.TransferService",
        )
        if rc == 0:
            logger.info(
                "start_service: TransferService started on %s", self._serial
            )
        else:
            logger.error(
                "start_service: failed (rc=%d) on %s: %s", rc, self._serial, stderr
            )
        return rc == 0

    def launch_main_activity(self) -> bool:
        """
        Bring the companion's MainActivity to the foreground via ADB so the
        user sees the permission-grant screen.

        This is required after a fresh ADB install because ``adb install``
        grants zero runtime permissions.  The user must open the app and tap
        through the permission dialogs (including the MANAGE_EXTERNAL_STORAGE
        Settings screen) before the TransferService can start its socket.

        Returns
        -------
        ``True`` if the ``am start`` command returned exit code 0.
        """
        _stdout, stderr, rc = self._adb.shell(
            self._serial,
            f"am start -n {COMPANION_PACKAGE}/.MainActivity",
        )
        if rc == 0:
            logger.info(
                "launch_main_activity: MainActivity started on %s", self._serial
            )
        else:
            logger.warning(
                "launch_main_activity: failed (rc=%d) on %s: %s",
                rc, self._serial, stderr,
            )
        return rc == 0

    def is_socket_ready(self) -> bool:
        """
        Check whether the companion's TCP socket is accepting connections.

        Attempts a short-timeout TCP connect to ``localhost:COMPANION_PORT``
        (which ADB forwards to the device).  A successful connect means
        ``TransferService`` is running *and* all required permissions have
        been granted — the socket is only opened after the service starts,
        and the service only starts after permissions are in place.

        :meth:`ensure_forward` must have been called before this method so
        the ADB forward rule exists on the host.

        Returns
        -------
        ``True`` if the socket accepted the connection.
        """
        try:
            with _socket_mod.create_connection(
                ("127.0.0.1", COMPANION_PORT), timeout=1.5
            ):
                pass
            logger.debug(
                "is_socket_ready: port %d accepting connections", COMPANION_PORT
            )
            return True
        except OSError:
            logger.debug("is_socket_ready: port %d not ready", COMPANION_PORT)
            return False

    def wait_until_socket_ready(
        self,
        timeout_seconds: int = 300,
        poll_interval: float = 3.0,
    ) -> bool:
        """
        Poll :meth:`is_socket_ready` until the companion's TCP socket accepts
        connections or the timeout is reached.

        Call :meth:`ensure_forward` and :meth:`launch_main_activity` before
        this method so the user sees the permission screen and the port
        forward is in place.

        Parameters
        ----------
        timeout_seconds:
            Maximum seconds to wait.  Default 300 (5 minutes) — enough for
            the user to navigate through Settings and grant
            ``MANAGE_EXTERNAL_STORAGE`` and ``REQUEST_INSTALL_PACKAGES``.
        poll_interval:
            Seconds between connection attempts.

        Returns
        -------
        ``True`` if the socket became ready before the timeout.
        """
        logger.info(
            "wait_until_socket_ready: waiting up to %ds for port %d on %s",
            timeout_seconds, COMPANION_PORT, self._serial,
        )
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            if self.is_socket_ready():
                logger.info(
                    "wait_until_socket_ready: companion socket ready on %s",
                    self._serial,
                )
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sleep_time = min(poll_interval, remaining)
            logger.debug(
                "wait_until_socket_ready: not ready yet, sleeping %.1fs (serial=%s)",
                sleep_time, self._serial,
            )
            time.sleep(sleep_time)

        logger.error(
            "wait_until_socket_ready: timed out after %ds for %s",
            timeout_seconds, self._serial,
        )
        return False

    def restart(self) -> bool:
        """
        Full restart cycle: force-stop → sleep 1.5 s → start service →
        sleep 2 s → re-establish ADB forward → check running.

        Returns
        -------
        ``True`` if the APK is running after the restart cycle.
        """
        logger.info(
            "restart: beginning restart cycle for %s on %s",
            COMPANION_PACKAGE, self._serial,
        )

        self.force_stop()
        time.sleep(1.5)

        self.start_service()
        time.sleep(2.0)

        self.ensure_forward()

        running = self.is_running()
        if running:
            logger.info(
                "restart: %s is running again on %s",
                COMPANION_PACKAGE, self._serial,
            )
        else:
            logger.error(
                "restart: %s did NOT come back up on %s",
                COMPANION_PACKAGE, self._serial,
            )
        return running

    # ------------------------------------------------------------------
    # Port forwarding
    # ------------------------------------------------------------------

    def ensure_forward(self) -> bool:
        """
        (Re-)establish the ADB TCP port forward for the companion APK port.

        Maps ``localhost:COMPANION_PORT`` on the host to
        ``device:COMPANION_PORT``.

        Returns
        -------
        ``True`` if the forward was created successfully.
        """
        ok = self._adb.forward(self._serial, COMPANION_PORT, COMPANION_PORT)
        if ok:
            logger.debug(
                "ensure_forward: localhost:%d -> device:%d on %s",
                COMPANION_PORT, COMPANION_PORT, self._serial,
            )
        else:
            logger.error(
                "ensure_forward: failed to (re-)establish forward on port %d for %s",
                COMPANION_PORT, self._serial,
            )
        return ok

    # ------------------------------------------------------------------
    # Polling helper
    # ------------------------------------------------------------------

    def wait_until_ready(self, timeout_seconds: int = 30) -> bool:
        """
        Poll :meth:`is_running` every 2 seconds until it returns ``True`` or
        the *timeout_seconds* deadline is reached.

        Parameters
        ----------
        timeout_seconds:
            Maximum seconds to wait.

        Returns
        -------
        ``True`` if the APK is running before the timeout expires.
        """
        logger.info(
            "wait_until_ready: waiting up to %ds for %s on %s",
            timeout_seconds, COMPANION_PACKAGE, self._serial,
        )
        deadline = time.monotonic() + timeout_seconds
        poll_interval = 2.0

        while time.monotonic() < deadline:
            if self.is_running():
                logger.info(
                    "wait_until_ready: %s is ready on %s",
                    COMPANION_PACKAGE, self._serial,
                )
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sleep_time = min(poll_interval, remaining)
            logger.debug(
                "wait_until_ready: not ready yet, sleeping %.1fs (serial=%s)",
                sleep_time, self._serial,
            )
            time.sleep(sleep_time)

        logger.error(
            "wait_until_ready: timed out after %ds for %s on %s",
            timeout_seconds, COMPANION_PACKAGE, self._serial,
        )
        return False


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------


def restart_companion(serial: str, adb: "ADBManager") -> bool:
    """
    Convenience wrapper: create a :class:`ProcessRestarter` and call
    :meth:`~ProcessRestarter.restart`.

    Parameters
    ----------
    serial:
        ADB device serial string.
    adb:
        An initialised :class:`~core.adb_manager.ADBManager` instance.

    Returns
    -------
    ``True`` if the companion APK is running after the restart.
    """
    return ProcessRestarter(serial, adb).restart()
