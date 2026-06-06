"""
afc_connector.py

File operations on iOS via the standard AFC (Apple File Conduit) service.

The standard AFC share is accessible on any iOS device without a jailbreak.
Its root maps to the device's /var/mobile/Media directory, which contains
DCIM, Photos, Downloads, and other user-accessible folders.

This module sits on top of ios_service_broker.IOSServiceBroker and exposes
a clean, Path-style API that hides the pymobiledevice3 service object.

pymobiledevice3 API compatibility
----------------------------------
pymobiledevice3 9.x made all AfcService methods async and removed the
open() context manager in favour of push()/pull()/get_file_contents()/
set_file_contents().  The _run() helper below detects coroutines and
executes them synchronously so the rest of the codebase stays sync.
Each method falls back gracefully across the following API generations:
  • pmd3 <9.x  — open() context manager (sync)
  • pmd3 9.x+  — push()/pull()/get_file_contents()/set_file_contents() (async)

Usage
-----
    from core.ios_service_broker import IOSServiceBroker
    from core.afc_connector import AFCConnector

    broker = IOSServiceBroker(udid="abc123...")
    afc = AFCConnector(broker)

    for name in afc.list_dir("/DCIM"):
        print(name)

    data = afc.read_file("/DCIM/100APPLE/IMG_0001.JPG")
    afc.pull_file("/DCIM/100APPLE/IMG_0001.JPG", Path("/tmp/img.jpg"))
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.ios_service_broker import IOSServiceBroker
from core.pmd3_asyncio import pmd3_run

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Async-compat helper
# ---------------------------------------------------------------------------

def _run(result: Any) -> Any:
    """
    If *result* is a coroutine (pmd3 9.x made all AFC methods async),
    run it on the shared persistent pmd3 event loop.  Otherwise return it directly.
    Using pmd3_run (not asyncio.run) keeps all pmd3 objects on the same loop.
    """
    return pmd3_run(result)


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

class AFCConnector:
    """
    Wraps the standard AFC service for file operations on /var/mobile/Media.

    All methods return sensible defaults (empty list, None, False) on error
    rather than propagating exceptions, so callers can write straightforward
    iteration code without per-call try/except blocks.
    """

    def __init__(self, broker: IOSServiceBroker) -> None:
        self._broker = broker
        # Eagerly validate the service is reachable.
        # get_afc() raises RuntimeError if it cannot connect.
        self._svc: Any = broker.get_afc()
        logger.debug("AFCConnector ready for UDID %s", broker.udid)

    # -----------------------------------------------------------------------
    # Directory operations
    # -----------------------------------------------------------------------

    def list_dir(self, path: str) -> list[str]:
        """
        List entries in *path* (relative to the AFC root /var/mobile/Media).
        Returns an empty list if the path does not exist or an error occurs.
        """
        self._ensure_service()
        try:
            result = _run(self._svc.listdir(path))
            if result is None:
                return []
            # Some pymobiledevice3 versions return bytes items
            return [
                item.decode("utf-8", errors="replace") if isinstance(item, bytes) else item
                for item in result
            ]
        except Exception as exc:
            logger.debug("AFC listdir(%s) failed: %s", path, exc)
            return []

    def makedirs(self, path: str) -> bool:
        """
        Create *path* and all parent directories on the device.
        Returns True on success or if the directory already exists.
        """
        self._ensure_service()
        try:
            _run(self._svc.makedirs(path))
            return True
        except Exception as exc:
            # Ignore "already exists" errors
            err = str(exc).lower()
            if "exist" in err or "already" in err:
                return True
            logger.warning("AFC makedirs(%s) failed: %s", path, exc)
            return False

    # -----------------------------------------------------------------------
    # Stat / existence
    # -----------------------------------------------------------------------

    def stat(self, path: str) -> dict | None:
        """
        Return file info dict for *path*, or None if it does not exist.

        The dict keys mirror AFC stat keys: st_size, st_mtime, st_birthtime,
        st_blocks, st_nlink, st_ifmt (AFC file type string).
        """
        self._ensure_service()
        try:
            info = _run(self._svc.stat(path))
            return info
        except Exception as exc:
            logger.debug("AFC stat(%s) failed: %s", path, exc)
            return None

    def exists(self, path: str) -> bool:
        """Return True if *path* exists on the device."""
        return self.stat(path) is not None

    # -----------------------------------------------------------------------
    # Read / write (small files — loads entirely into memory)
    # -----------------------------------------------------------------------

    def read_file(self, path: str) -> bytes | None:
        """
        Read and return the full content of *path* as bytes.
        Returns None on error (file not found, permission denied, etc.).
        """
        self._ensure_service()
        try:
            if hasattr(self._svc, "open"):
                # pmd3 <9.x: open() returns a sync context manager
                with self._svc.open(path, "rb") as fh:
                    return fh.read()
            else:
                # pmd3 9.x+: get_file_contents() is async
                return _run(self._svc.get_file_contents(path))
        except Exception as exc:
            logger.debug("AFC read_file(%s) failed: %s", path, exc)
            return None

    def write_file(self, path: str, data: bytes) -> bool:
        """
        Write *data* to *path* on the device, overwriting if it exists.
        Returns True on success.
        """
        self._ensure_service()
        try:
            if hasattr(self._svc, "open"):
                # pmd3 <9.x
                with self._svc.open(path, "wb") as fh:
                    fh.write(data)
            else:
                # pmd3 9.x+
                _run(self._svc.set_file_contents(path, data))
            return True
        except Exception as exc:
            logger.warning("AFC write_file(%s) failed: %s", path, exc)
            return False

    # -----------------------------------------------------------------------
    # Pull / push (large-file streaming)
    # -----------------------------------------------------------------------

    def pull_file(self, device_path: str, local_path: Path) -> bool:
        """
        Download a file from the device to the local filesystem.

        Tries in order:
          • pmd3 <9.x : open() context manager with 1 MiB streaming chunks
          • pmd3 9.x+ : pull(remote, local) which takes filesystem paths
          • Fallback  : get_file_contents() — loads whole file into memory
        Returns True on success.
        """
        self._ensure_service()
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)

            if hasattr(self._svc, "open"):
                # pmd3 <9.x: streaming via open()
                with self._svc.open(device_path, "rb") as src:
                    with local_path.open("wb") as dst:
                        while True:
                            chunk = src.read(1 << 20)  # 1 MiB
                            if not chunk:
                                break
                            dst.write(chunk)

            elif hasattr(self._svc, "pull") and callable(self._svc.pull):
                # pmd3 9.x+: pull(remote, local) — takes filesystem paths
                _run(self._svc.pull(device_path, str(local_path)))

            else:
                # Ultimate fallback: get_file_contents (loads whole file into RAM)
                data = _run(self._svc.get_file_contents(device_path))
                if data is None:
                    logger.warning("AFC pull_file: get_file_contents returned None for %s", device_path)
                    return False
                local_path.write_bytes(data)

            logger.debug("AFC pull: %s -> %s", device_path, local_path)
            return True
        except Exception as exc:
            logger.warning("AFC pull_file(%s) failed: %s", device_path, exc)
            return False

    def push_file(self, local_path: Path, device_path: str) -> bool:
        """
        Upload a local file to the device.

        Tries in order:
          • pmd3 <9.x : open() context manager with 1 MiB streaming chunks
          • pmd3 9.x+ : push(local, remote) which takes filesystem paths
          • Fallback  : set_file_contents() — loads whole file into memory
        Returns True on success.
        """
        self._ensure_service()
        if not local_path.exists():
            logger.error("AFC push_file: local file not found: %s", local_path)
            return False
        try:
            if hasattr(self._svc, "open"):
                # pmd3 <9.x: streaming via open()
                with local_path.open("rb") as src:
                    with self._svc.open(device_path, "wb") as dst:
                        while True:
                            chunk = src.read(1 << 20)  # 1 MiB
                            if not chunk:
                                break
                            dst.write(chunk)

            elif hasattr(self._svc, "push") and callable(self._svc.push):
                # pmd3 9.x+: push(local, remote) — takes filesystem paths
                _run(self._svc.push(str(local_path), device_path))

            else:
                # Ultimate fallback: set_file_contents (loads whole file into RAM)
                data = local_path.read_bytes()
                _run(self._svc.set_file_contents(device_path, data))

            logger.debug("AFC push: %s -> %s", local_path, device_path)
            return True
        except Exception as exc:
            logger.warning("AFC push_file(%s) failed: %s", local_path, exc)
            return False

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    def _ensure_service(self) -> None:
        """
        Verify the AFC service handle is alive; reconnect if needed.
        Raises RuntimeError if reconnection fails.
        """
        if self._svc is None:
            logger.debug("AFC service None — attempting reconnect")
            self._svc = self._broker.get_afc()
