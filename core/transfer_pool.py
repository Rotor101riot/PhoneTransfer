"""
transfer_pool.py

Pool of N parallel CompanionClient connections for concurrent file transfers.

When the companion app accepts multiple connections (SocketServer v2), the PC
side can distribute file push/pull operations across several connections to
overlap USB/network I/O with device-side storage writes.  This is the primary
mechanism for matching Dr.Fone-class transfer speeds.

Typical usage::

    from core.transfer_pool import TransferPool

    with TransferPool(max_workers=4) as pool:
        results = pool.parallel_file_push(file_specs)
        failed = [r for r in results if not r["ok"]]

For single-file operations or structured-data commands, continue using a
regular :class:`CompanionClient` directly.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Default number of parallel connections.
_DEFAULT_WORKERS = 4


class TransferPool:
    """
    Pool of parallel CompanionClient connections for concurrent file I/O.

    Parameters
    ----------
    host:
        Target host (usually ``"127.0.0.1"`` with ADB port forwarding).
    port:
        Target port (default 7337).
    max_workers:
        Number of concurrent connections to open.  The companion app
        supports up to 4 by default.
    timeout:
        Per-socket timeout in seconds.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7337,
        max_workers: int = _DEFAULT_WORKERS,
        timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._max_workers = max_workers
        self._timeout = timeout
        self._clients: list = []
        self._pool: ThreadPoolExecutor | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open N connections to the companion app and start the thread pool."""
        from core.companion_app_protocol import CompanionClient

        for i in range(self._max_workers):
            try:
                client = CompanionClient(
                    host=self._host,
                    port=self._port,
                    timeout=self._timeout,
                )
                client.connect()
                self._clients.append(client)
                logger.debug(
                    "transfer_pool: connection %d/%d established (v%d)",
                    i + 1, self._max_workers, client.protocol_version,
                )
            except Exception as exc:
                logger.warning(
                    "transfer_pool: connection %d/%d failed: %s — "
                    "continuing with %d workers",
                    i + 1, self._max_workers, exc, len(self._clients),
                )
                break  # companion may not support this many connections

        if not self._clients:
            raise ConnectionError(
                "transfer_pool: could not open any connections to "
                f"{self._host}:{self._port}"
            )

        self._pool = ThreadPoolExecutor(
            max_workers=len(self._clients),
            thread_name_prefix="xfer",
        )
        logger.info(
            "transfer_pool: ready with %d parallel connections",
            len(self._clients),
        )

    def close(self) -> None:
        """Disconnect all clients and shut down the thread pool."""
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None

        for client in self._clients:
            try:
                client.disconnect()
            except Exception:
                pass
        self._clients.clear()
        logger.debug("transfer_pool: closed")

    def __enter__(self) -> "TransferPool":
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Parallel file push
    # ------------------------------------------------------------------

    def parallel_file_push(
        self,
        file_specs: list[dict[str, Any]],
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Push multiple files concurrently across the pool connections.

        Parameters
        ----------
        file_specs:
            List of dicts, each with at minimum:
            - ``local_path`` (Path): local file to push
            - ``dest`` (str): destination type ("photos", "videos", "downloads")
            Optionally:
            - ``date_taken`` (int | None): epoch ms for media metadata
            - ``resumable`` (bool): use resumable push if True
            - ``server_has`` (int): bytes already on server for resume
        progress_callback:
            Called with ``(completed_count, total_count)`` after each file.

        Returns
        -------
        list[dict]
            One result per file_spec, in the same order.  Each result is
            the companion's done-frame dict, with an added ``"ok"`` key
            (True if push succeeded).
        """
        if self._pool is None:
            raise RuntimeError("TransferPool is not open — call open() first")

        n_clients = len(self._clients)
        total = len(file_specs)
        results: list[dict[str, Any] | None] = [None] * total
        completed = 0

        # Submit all pushes, round-robin across connections
        future_to_idx: dict[Future, int] = {}
        for idx, spec in enumerate(file_specs):
            client = self._clients[idx % n_clients]
            future = self._pool.submit(
                self._push_one, client, spec,
            )
            future_to_idx[future] = idx

        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                result = future.result()
                result["ok"] = True
                results[idx] = result
            except Exception as exc:
                logger.warning(
                    "transfer_pool: push failed for %s: %s",
                    file_specs[idx].get("local_path", "?"), exc,
                )
                results[idx] = {"ok": False, "error": str(exc)}

            completed += 1
            if progress_callback is not None:
                progress_callback(completed, total)

        return results  # type: ignore[return-value]

    @staticmethod
    def _push_one(client: Any, spec: dict[str, Any]) -> dict:
        """Push a single file using the given CompanionClient."""
        local_path = Path(spec["local_path"])
        dest = spec.get("dest", "downloads")
        date_taken = spec.get("date_taken")

        if spec.get("resumable"):
            return client.file_push_resumable(
                local_path=local_path,
                dest=dest,
                date_taken=date_taken,
                server_has=spec.get("server_has", 0),
                progress_callback=spec.get("file_progress_callback"),
            )
        return client.file_push(
            local_path=local_path,
            dest=dest,
            date_taken=date_taken,
        )

    # ------------------------------------------------------------------
    # Parallel file pull
    # ------------------------------------------------------------------

    def parallel_file_pull(
        self,
        file_specs: list[dict[str, Any]],
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Pull multiple files concurrently across the pool connections.

        Parameters
        ----------
        file_specs:
            List of dicts, each with:
            - ``remote_path`` (str): path on device
            - ``local_path`` (Path): destination on PC
            Optionally:
            - ``resumable`` (bool): use resumable pull if True
        progress_callback:
            Called with ``(completed_count, total_count)`` after each file.

        Returns
        -------
        list[dict]
            One result per spec, with an ``"ok"`` key.
        """
        if self._pool is None:
            raise RuntimeError("TransferPool is not open — call open() first")

        n_clients = len(self._clients)
        total = len(file_specs)
        results: list[dict[str, Any] | None] = [None] * total
        completed = 0

        future_to_idx: dict[Future, int] = {}
        for idx, spec in enumerate(file_specs):
            client = self._clients[idx % n_clients]
            future = self._pool.submit(
                self._pull_one, client, spec,
            )
            future_to_idx[future] = idx

        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                result = future.result()
                result["ok"] = True
                results[idx] = result
            except Exception as exc:
                logger.warning(
                    "transfer_pool: pull failed for %s: %s",
                    file_specs[idx].get("remote_path", "?"), exc,
                )
                results[idx] = {"ok": False, "error": str(exc)}

            completed += 1
            if progress_callback is not None:
                progress_callback(completed, total)

        return results  # type: ignore[return-value]

    @staticmethod
    def _pull_one(client: Any, spec: dict[str, Any]) -> dict:
        """Pull a single file using the given CompanionClient."""
        remote_path = spec["remote_path"]
        local_path = Path(spec["local_path"])

        if spec.get("resumable"):
            return client.file_pull_resumable(
                remote_path=remote_path,
                local_path=local_path,
            )
        return client.file_pull(
            remote_path=remote_path,
            local_path=local_path,
        )
