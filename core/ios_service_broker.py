"""
ios_service_broker.py

Manages pymobiledevice3 service connections for a single iOS device (UDID).

Responsibilities
----------------
- Owns the LockdownClient lifecycle: creates it on first use, reconnects
  transparently if the connection drops.
- Provides typed accessor methods for commonly needed services (AFC, AFC2,
  installation proxy, notification proxy).
- Wraps every pymobiledevice3 call in try/except so callers never receive
  raw pymobiledevice3 exceptions; None is returned for optional services,
  RuntimeError is raised only for truly unrecoverable situations.

Usage
-----
    from core.config_loader import get_config
    from core.ios_service_broker import IOSServiceBroker

    broker = IOSServiceBroker(udid="abc123...", config=get_config())
    lockdown = broker.get_lockdown()
    afc = broker.get_afc()
    version = broker.query_lockdown("ProductVersion")
    broker.close()

Note on pymobiledevice3 API versioning
---------------------------------------
The pymobiledevice3 library has evolved its import paths and constructor
signatures across versions.  This module uses try/except ImportError blocks
to handle the most common variations without breaking on either old or new
installs.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from core.config_loader import Config, get_config
from core.pmd3_asyncio import pmd3_run

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# pymobiledevice3 import shims
# (adjust fallbacks as the upstream API evolves)
# ---------------------------------------------------------------------------

def _create_lockdown(udid: str) -> Any:
    """
    Create and return a live lockdown client for *udid*.

    pymobiledevice3 9.x made LockdownClient abstract and introduced the
    create_using_usbmux() factory.  We try in order:
      1. create_using_usbmux(serial=udid)   — pmd3 9.x+
      2. LockdownClient(serial=udid)         — pmd3 4–8.x
      3. LockdownClient(udid)               — pmd3 <4.x
    Raises RuntimeError if none succeed.
    """
    # ── Attempt 1: modern factory (pmd3 9.x) ───────────────────────────────
    # Note: create_using_usbmux is itself async in pmd3 9.x, so we must
    # check for a coroutine and run it synchronously.
    try:
        from pymobiledevice3.lockdown import create_using_usbmux  # type: ignore[import]
        result = create_using_usbmux(serial=udid)
        if inspect.iscoroutine(result):
            result = pmd3_run(result)
        return result
    except ImportError:
        pass  # symbol doesn't exist in this version
    except Exception as exc:
        logger.debug("create_using_usbmux failed for %s: %s", udid, exc)

    # ── Attempt 2: keyword-arg constructor (pmd3 4–8.x) ────────────────────
    try:
        from pymobiledevice3.lockdown import LockdownClient  # type: ignore[import]
        result = LockdownClient(serial=udid)
        if inspect.iscoroutine(result):
            result = pmd3_run(result)
        return result
    except ImportError:
        pass
    except TypeError:
        pass  # no 'serial' kwarg → fall through to positional attempt
    except Exception as exc:
        logger.debug("LockdownClient(serial=) failed for %s: %s", udid, exc)

    # ── Attempt 3: positional constructor (pmd3 <4.x) ──────────────────────
    try:
        from pymobiledevice3.lockdown import LockdownClient  # type: ignore[import]
        result = LockdownClient(udid)  # type: ignore[call-arg]
        if inspect.iscoroutine(result):
            result = pmd3_run(result)
        return result
    except ImportError:
        raise RuntimeError(
            "pymobiledevice3 LockdownClient is not importable — "
            "iOS service connections will fail."
        )
    except Exception as exc:
        raise RuntimeError(
            f"LockdownClient creation failed for {udid}: {exc}"
        ) from exc


def _import_afc_service() -> type | None:
    """Return the AfcService class or None if unavailable."""
    try:
        from pymobiledevice3.services.afc import AfcService  # type: ignore[import]
        return AfcService
    except Exception:
        pass
    try:
        from pymobiledevice3.afc import AfcService  # type: ignore[import]
        return AfcService
    except Exception:
        pass
    # Newer pymobiledevice3 (4.x+) may reorganise under services.base_service
    try:
        from pymobiledevice3.services import afc as _afc_mod  # type: ignore[import]
        cls = getattr(_afc_mod, "AfcService", None)
        if cls is not None:
            return cls
    except Exception:
        pass
    logger.error(
        "pymobiledevice3 AfcService not importable. "
        "Run: pip install --upgrade pymobiledevice3"
    )
    return None


def _import_installation_proxy() -> type | None:
    try:
        from pymobiledevice3.services.installation_proxy import (  # type: ignore[import]
            InstallationProxyService,
        )
        return InstallationProxyService
    except ImportError:
        pass
    try:
        from pymobiledevice3.installation_proxy import InstallationProxyService  # type: ignore[import]
        return InstallationProxyService
    except ImportError:
        return None


def _import_notification_proxy() -> type | None:
    try:
        from pymobiledevice3.services.notification_proxy import (  # type: ignore[import]
            NotificationProxyService,
        )
        return NotificationProxyService
    except ImportError:
        pass
    try:
        from pymobiledevice3.notification_proxy import NotificationProxyService  # type: ignore[import]
        return NotificationProxyService
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Broker class
# ---------------------------------------------------------------------------

class IOSServiceBroker:
    """
    Connection manager for all pymobiledevice3 services on a single device.

    The broker is designed to be long-lived for the duration of a transfer
    session.  Services are opened on demand and cached where safe to do so.
    Call close() when finished to release all resources.
    """

    def __init__(self, udid: str, config: Config | None = None) -> None:
        self.udid = udid
        self._cfg = config or get_config()

        # Cached service handles (None until first use)
        self._lockdown: Any = None
        self._afc: Any = None
        self._afc2: Any = None

    # -----------------------------------------------------------------------
    # Lockdown
    # -----------------------------------------------------------------------

    def get_lockdown(self) -> Any:
        """
        Return a live LockdownClient.  Creates or reconnects as needed.
        Raises RuntimeError if pymobiledevice3 is not available.
        """
        if self._lockdown is not None and self._is_lockdown_alive():
            return self._lockdown

        logger.debug("Opening LockdownClient for UDID %s", self.udid)
        self._lockdown = _create_lockdown(self.udid)  # raises RuntimeError on failure
        logger.debug("LockdownClient ready for %s", self.udid)
        return self._lockdown

    def _is_lockdown_alive(self) -> bool:
        """Quick sanity check: try a lightweight lockdown query."""
        try:
            # A no-op key query is the cheapest liveness check
            if hasattr(self._lockdown, "get_value"):
                result = self._lockdown.get_value("ProductVersion")
                if inspect.iscoroutine(result):
                    pmd3_run(result)
            elif hasattr(self._lockdown, "get"):
                result = self._lockdown.get("ProductVersion")
                if inspect.iscoroutine(result):
                    pmd3_run(result)
            return True
        except Exception:
            logger.debug(
                "Lockdown connection for %s appears dead, will reconnect", self.udid
            )
            self._lockdown = None
            return False

    # -----------------------------------------------------------------------
    # AFC (standard — /var/mobile/Media)
    # -----------------------------------------------------------------------

    def get_afc(self) -> Any:
        """
        Return an AfcService client for the standard AFC share
        (accessible without jailbreak: /var/mobile/Media).

        Returns the client object on success.
        Raises RuntimeError if the service cannot be opened.
        """
        if self._afc is not None:
            return self._afc

        AfcService = _import_afc_service()
        if AfcService is None:
            raise RuntimeError("AfcService not importable from pymobiledevice3.")

        lockdown = self.get_lockdown()
        try:
            logger.debug("Opening AFC service for %s", self.udid)
            svc = AfcService(lockdown)
            if inspect.iscoroutine(svc):
                svc = pmd3_run(svc)
            self._afc = svc
            return self._afc
        except Exception as exc:
            logger.error("Failed to open AFC service for %s: %s", self.udid, exc)
            raise RuntimeError(f"AFC service failed for {self.udid}: {exc}") from exc

    # -----------------------------------------------------------------------
    # AFC2 (full filesystem — jailbreak only)
    # -----------------------------------------------------------------------

    def get_afc2(self) -> Any | None:
        """
        Return an AfcService client for the AFC2 share (full filesystem).
        Returns None if the device is not jailbroken or AFC2 is not installed.
        Does not raise.
        """
        if self._afc2 is not None:
            return self._afc2

        AfcService = _import_afc_service()
        if AfcService is None:
            logger.warning("AfcService not importable — cannot open AFC2.")
            return None

        try:
            lockdown = self.get_lockdown()
        except RuntimeError as exc:
            logger.warning("Cannot get lockdown for AFC2: %s", exc)
            return None

        try:
            logger.debug("Opening AFC2 service for %s", self.udid)
            svc2 = AfcService(lockdown, service_name="com.apple.afc2")
            if inspect.iscoroutine(svc2):
                svc2 = pmd3_run(svc2)
            self._afc2 = svc2
            logger.info("AFC2 service opened for %s", self.udid)
            return self._afc2
        except Exception as exc:
            logger.debug(
                "AFC2 not available for %s (device likely not jailbroken): %s",
                self.udid, exc,
            )
            return None

    # -----------------------------------------------------------------------
    # Installation proxy
    # -----------------------------------------------------------------------

    def get_installation_proxy(self) -> Any:
        """
        Return an InstallationProxyService for the device.
        Raises RuntimeError on failure.
        """
        InstallationProxyService = _import_installation_proxy()
        if InstallationProxyService is None:
            raise RuntimeError(
                "InstallationProxyService not importable from pymobiledevice3."
            )

        lockdown = self.get_lockdown()
        try:
            logger.debug("Opening InstallationProxyService for %s", self.udid)
            svc = InstallationProxyService(lockdown)
            if inspect.iscoroutine(svc):
                svc = pmd3_run(svc)
            return svc
        except Exception as exc:
            logger.error(
                "Failed to open InstallationProxyService for %s: %s", self.udid, exc
            )
            raise RuntimeError(
                f"InstallationProxyService failed for {self.udid}: {exc}"
            ) from exc

    # -----------------------------------------------------------------------
    # Notification proxy
    # -----------------------------------------------------------------------

    def get_notification_proxy(self) -> Any:
        """
        Return a NotificationProxyService for the device.
        Raises RuntimeError on failure.
        """
        NotificationProxyService = _import_notification_proxy()
        if NotificationProxyService is None:
            raise RuntimeError(
                "NotificationProxyService not importable from pymobiledevice3."
            )

        lockdown = self.get_lockdown()
        try:
            logger.debug("Opening NotificationProxyService for %s", self.udid)
            svc = NotificationProxyService(lockdown)
            if inspect.iscoroutine(svc):
                svc = pmd3_run(svc)
            return svc
        except Exception as exc:
            logger.error(
                "Failed to open NotificationProxyService for %s: %s", self.udid, exc
            )
            raise RuntimeError(
                f"NotificationProxyService failed for {self.udid}: {exc}"
            ) from exc

    def reboot_device(self) -> bool:
        """
        Send a reboot command to the device via DiagnosticsService.
        Returns True if the command was sent successfully, False otherwise.
        The device will disconnect within a few seconds of this call returning.
        """
        try:
            from pymobiledevice3.services.diagnostics import DiagnosticsService  # type: ignore[import]
        except ImportError:
            logger.warning("DiagnosticsService not available — cannot reboot device")
            return False
        try:
            lockdown = self.get_lockdown()
            svc = DiagnosticsService(lockdown)
            if inspect.iscoroutine(svc):
                svc = pmd3_run(svc)
            result = svc.restart()
            if inspect.iscoroutine(result):
                pmd3_run(result)
            logger.info("reboot_device: reboot command sent to %s", self.udid)
            return True
        except Exception as exc:
            logger.warning("reboot_device: failed for %s: %s", self.udid, exc)
            return False

    # -----------------------------------------------------------------------
    # Convenience query
    # -----------------------------------------------------------------------

    def get_ios_version(self) -> str | None:
        """
        Return the device's iOS version string (e.g. '17.2.1'), or None on failure.
        Thin convenience wrapper around query_lockdown('ProductVersion').
        """
        version = self.query_lockdown("ProductVersion")
        if version:
            return str(version)
        return None

    def query_lockdown(self, key: str) -> Any:
        """
        Query a single lockdown key (e.g. 'ProductVersion', 'DeviceName').
        Returns the value or None if the key is unavailable or an error occurs.
        """
        try:
            lockdown = self.get_lockdown()
            if hasattr(lockdown, "get_value"):
                result = lockdown.get_value(key)
                return pmd3_run(result) if inspect.iscoroutine(result) else result
            if hasattr(lockdown, "get"):
                result = lockdown.get(key)
                return pmd3_run(result) if inspect.iscoroutine(result) else result
            # Some versions expose values as attributes on an all_values dict
            if hasattr(lockdown, "all_values") and isinstance(lockdown.all_values, dict):
                return lockdown.all_values.get(key)
            return None
        except Exception as exc:
            logger.debug("query_lockdown(%s) failed for %s: %s", key, self.udid, exc)
            return None

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def close(self) -> None:
        """Release all open service connections."""
        for attr, label in (
            ("_afc2", "AFC2"),
            ("_afc", "AFC"),
            ("_lockdown", "Lockdown"),
        ):
            svc = getattr(self, attr, None)
            if svc is not None:
                try:
                    result = svc.close()  # type: ignore[attr-defined]
                    # pmd3 9.x made close() async on some service classes
                    if inspect.iscoroutine(result):
                        pmd3_run(result)
                    logger.debug("%s closed for %s", label, self.udid)
                except Exception as exc:
                    logger.debug(
                        "Error closing %s for %s (ignored): %s", label, self.udid, exc
                    )
                setattr(self, attr, None)

    def __enter__(self) -> "IOSServiceBroker":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"IOSServiceBroker(udid={self.udid!r})"
