"""
pmd3_asyncio.py

Shared persistent asyncio event loop for pymobiledevice3 operations.

Why a persistent loop?
-----------------------
pymobiledevice3 9.x made its entire API async.  Our codebase is synchronous,
so we bridge via a single long-lived event loop rather than asyncio.run().

asyncio.run() creates a *new* event loop on every call.  pmd3 service objects
(LockdownClient, AfcService) bind their sockets and internal Futures to
whichever loop was running when they were constructed.  Running subsequent
async method calls through a *different* loop produces:

    "got Future <...> attached to a different loop"

Using one persistent loop (run_until_complete) for all pmd3 calls keeps
every service object on the same loop from creation to close.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from typing import Any

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """Return the shared persistent event loop, creating it if needed."""
    global _loop
    with _lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
        return _loop


def pmd3_run(result: Any) -> Any:
    """
    If *result* is a coroutine, run it on the shared persistent event loop
    and return the value.  If it is a plain value, return it unchanged.

    Use this everywhere instead of asyncio.run() for pymobiledevice3 calls.
    """
    if inspect.iscoroutine(result):
        return _get_loop().run_until_complete(result)
    return result
