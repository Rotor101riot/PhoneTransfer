"""
afc2_connector.py

File operations on iOS via AFC2 (Apple File Conduit 2) — full filesystem
access from the root ('/') downward.

AFC2 is only available on jailbroken devices with the AFC2 service installed
(typically provided by the AFC2 package available in Cydia/Sileo).  Unlike
the standard AFC share, AFC2 exposes the entire iOS filesystem and is
required for extracting data that lives outside /var/mobile/Media (e.g.
SMS databases, call history, notes — all under /private/var/mobile/).

This module delegates entirely to AFCConnector after swapping in the AFC2
service handle.  The public API is identical to AFCConnector so that callers
can use either interchangeably.

Usage
-----
    from core.ios_service_broker import IOSServiceBroker
    from core.afc2_connector import AFC2Connector

    broker = IOSServiceBroker(udid="abc123...")
    afc2 = AFC2Connector(broker)   # raises PermissionError if not jailbroken

    data = afc2.read_file("/private/var/mobile/Library/SMS/sms.db")
"""

from __future__ import annotations

import logging
from typing import Any

from core.ios_service_broker import IOSServiceBroker
from core.afc_connector import AFCConnector

logger = logging.getLogger(__name__)


class AFC2Connector(AFCConnector):
    """
    Identical interface to AFCConnector but operating over the AFC2 service
    (full filesystem access, root '/').

    Raises PermissionError at construction time if the AFC2 service is not
    available on the target device.
    """

    def __init__(self, broker: IOSServiceBroker) -> None:
        # Do NOT call super().__init__() — we set up the service ourselves
        # so we can use the AFC2 handle rather than the standard AFC handle.
        self._broker = broker

        afc2_svc = broker.get_afc2()
        if afc2_svc is None:
            raise PermissionError(
                f"AFC2 is not available on device {broker.udid!r}.  "
                "The device must be jailbroken and have the AFC2 service "
                "installed (via Cydia/Sileo 'Apple File Conduit 2' package) "
                "before full-filesystem access is possible."
            )

        self._svc: Any = afc2_svc
        logger.info(
            "AFC2Connector ready — full filesystem access on UDID %s", broker.udid
        )

    # All file-operation methods (list_dir, stat, exists, read_file,
    # write_file, pull_file, push_file, makedirs) are inherited from
    # AFCConnector unchanged.  They reference self._svc which is now the
    # AFC2 service object.

    def _ensure_service(self) -> None:
        """
        Verify the AFC2 service handle is alive; reconnect if needed.
        Raises PermissionError if AFC2 becomes unavailable after initial connect.
        """
        if self._svc is None:
            logger.debug("AFC2 service None — attempting reconnect")
            afc2_svc = self._broker.get_afc2()
            if afc2_svc is None:
                raise PermissionError(
                    f"AFC2 service lost on device {self._broker.udid!r} and "
                    "could not be re-established.  Check that the device is "
                    "still connected and the jailbreak is still active."
                )
            self._svc = afc2_svc
            logger.info("AFC2 service reconnected for %s", self._broker.udid)
