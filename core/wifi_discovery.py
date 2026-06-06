"""
wifi_discovery.py

Discovers PhoneTransfer companion APK instances on the local network using
mDNS/Zeroconf and connects to them over Wi-Fi (no ADB USB required).

The companion APK advertises itself as:
    service type: _phonetransfer._tcp.local.
    port: 7337

Usage
-----
    from core.wifi_discovery import discover_companions, WifiCompanionSession

    # Discover devices on the LAN
    devices = discover_companions(timeout=5.0)
    for d in devices:
        print(d.name, d.host, d.port)

    # Connect to the first one found
    if devices:
        with WifiCompanionSession(devices[0]) as client:
            if client.ping():
                result = client.extract("contacts")

Requirements
------------
    pip install zeroconf
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_SERVICE_TYPE = "_phonetransfer._tcp.local."
_DEFAULT_PORT = 7337


# ---------------------------------------------------------------------------
# Discovery result
# ---------------------------------------------------------------------------

@dataclass
class CompanionDevice:
    """A companion APK instance discovered on the local network."""
    name: str          # mDNS service instance name (e.g. "Galaxy S24 (PhoneTransfer)")
    host: str          # resolved IP address
    port: int          # TCP port (usually 7337)
    properties: dict   # TXT record properties from the APK


# ---------------------------------------------------------------------------
# Discover
# ---------------------------------------------------------------------------

def discover_companions(timeout: float = 5.0) -> list[CompanionDevice]:
    """
    Browse the local network for PhoneTransfer companion APK instances.

    Parameters
    ----------
    timeout:
        How many seconds to wait for mDNS responses.

    Returns
    -------
    List of :class:`CompanionDevice` instances found; may be empty.
    """
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf  # type: ignore[import]
    except ImportError:
        logger.error(
            "wifi_discovery: zeroconf library is not installed. "
            "Install it with: pip install zeroconf"
        )
        return []

    found: list[CompanionDevice] = []
    lock = threading.Lock()

    class _Listener(ServiceListener):
        def add_service(self, zc: "Zeroconf", type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name)
            if info is None:
                return
            addresses = info.parsed_scoped_addresses()
            host = addresses[0] if addresses else None
            if host is None:
                return
            props: dict = {}
            for k, v in (info.properties or {}).items():
                key = k.decode() if isinstance(k, bytes) else str(k)
                val = v.decode() if isinstance(v, bytes) else (v or "")
                props[key] = val

            device = CompanionDevice(
                name       = info.name,
                host       = host,
                port       = info.port or _DEFAULT_PORT,
                properties = props,
            )
            with lock:
                found.append(device)
            logger.info(
                "wifi_discovery: found companion '%s' at %s:%d",
                device.name, device.host, device.port,
            )

        def remove_service(self, zc, type_, name):
            pass

        def update_service(self, zc, type_, name):
            pass

    zc = None
    listener = _Listener()
    try:
        zc = Zeroconf()
        browser = ServiceBrowser(zc, _SERVICE_TYPE, listener)  # noqa: F841
        time.sleep(timeout)
    finally:
        if zc is not None:
            zc.close()

    with lock:
        return list(found)


# ---------------------------------------------------------------------------
# Wi-Fi companion session (wraps CompanionClient with direct TCP connection)
# ---------------------------------------------------------------------------

class WifiCompanionSession:
    """
    A companion client connected directly over Wi-Fi (no ADB).

    Wraps :class:`~core.companion_app_protocol.CompanionClient` with
    the device IP obtained from :func:`discover_companions`.

    Parameters
    ----------
    device:
        A :class:`CompanionDevice` from :func:`discover_companions`.
    timeout:
        Per-operation socket timeout in seconds.
    """

    def __init__(self, device: CompanionDevice, timeout: float = 30.0) -> None:
        from core.companion_app_protocol import CompanionClient
        self._device = device
        self._client = CompanionClient(
            host    = device.host,
            port    = device.port,
            timeout = timeout,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        self._client.connect()
        logger.info(
            "WifiCompanionSession: connected to %s at %s:%d",
            self._device.name, self._device.host, self._device.port,
        )

    def disconnect(self) -> None:
        self._client.disconnect()

    def __enter__(self) -> "WifiCompanionSession":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    # ------------------------------------------------------------------
    # Delegate to underlying client
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        return self._client.ping()

    def capabilities(self) -> dict:
        return self._client.capabilities()

    def extract(self, category: str, **kwargs) -> dict:
        return self._client.extract(category, **kwargs)

    def inject(self, category: str, items: list, **kwargs) -> dict:
        return self._client.inject(category, items, **kwargs)

    def send(self, payload: dict) -> None:
        return self._client.send(payload)

    def recv(self) -> dict:
        return self._client.recv()

    def send_recv(self, payload: dict) -> dict:
        return self._client.send_recv(payload)

    @property
    def device(self) -> CompanionDevice:
        return self._device


# ---------------------------------------------------------------------------
# Convenience: discover + connect in one call
# ---------------------------------------------------------------------------

def connect_first(timeout: float = 5.0, socket_timeout: float = 30.0) -> WifiCompanionSession | None:
    """
    Discover companions and return a connected session to the first one found.

    Returns ``None`` if no device is discovered within *timeout* seconds.
    """
    devices = discover_companions(timeout=timeout)
    if not devices:
        logger.info("wifi_discovery: no companion devices found within %.1fs", timeout)
        return None
    session = WifiCompanionSession(devices[0], timeout=socket_timeout)
    session.connect()
    return session
