"""
device_connection_cache.py

Process-level caches for expensive iOS connection objects.

Problem
-------
Every extractor previously called ``iOSbackup(udid=udid)`` and
``IOSServiceBroker(udid=udid)`` inline.  Creating an ``iOSbackup`` instance
loads (and for encrypted backups, AES-decrypts) ``Manifest.db``; creating
an ``IOSServiceBroker`` performs a full USB lockdown handshake.  With 13+
extractors in a single session that overhead stacks up fast.

Solution
--------
Both objects are safe to share across sequential extractor calls for the same
UDID:

- ``iOSbackup`` keeps an open SQLite connection to Manifest.db and handles
  its own state internally.
- ``IOSServiceBroker`` already caches lockdown/AFC/AFC2 service handles
  internally; it just needs one instance per UDID.

Usage
-----
    from core.device_connection_cache import get_iosbackup, get_broker

    # Non-jailbroken path:
    backup = get_iosbackup(udid)
    info = backup.getFileDecryptedCopy(...)

    # Jailbroken path:
    broker = get_broker(udid)
    afc2 = AFC2Connector(broker)

Cleanup
-------
Call ``clear_connection_cache()`` at session end (e.g. from
``SessionManager.__exit__``) to close service handles and release resources.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal caches
# ---------------------------------------------------------------------------

_iosbackup_cache: dict[str, Any] = {}
_broker_cache: dict[str, Any] = {}

# Maps UDID → the MobileSync backup root (parent of the UDID folder).
# Populated by BackupManager after a successful backup/decrypt so that
# get_iosbackup() opens the right directory rather than the system default.
_backup_roots: dict[str, Path] = {}

# Maps UDID → cleartext backup password for encrypted backups.
# When set, get_iosbackup() passes cleartextpassword so iOSbackup decrypts on-the-fly.
_backup_passwords: dict[str, str] = {}


def register_backup_password(udid: str, password: str) -> None:
    """
    Register a cleartext backup password for *udid*.

    When set, ``get_iosbackup()`` passes ``cleartextpassword`` so that
    iOSbackup can decrypt an encrypted backup on-the-fly without requiring
    a separate pre-decryption step.
    """
    if _backup_passwords.get(udid) == password:
        return
    _backup_passwords[udid] = password
    # Evict any cached iOSbackup instance so next call re-opens with the password
    if udid in _iosbackup_cache:
        try:
            _iosbackup_cache[udid].close()
        except Exception:
            pass
        del _iosbackup_cache[udid]
    logger.debug("device_connection_cache: registered backup password for %s", udid)


def register_backup_dir(udid: str, backup_dir: Path) -> None:
    """
    Register the MobileSync backup directory for *udid*.

    ``backup_dir`` is the UDID subfolder (e.g. ``tmp/backups/{udid}``).
    Its parent (the MobileSync root) is what iOSbackup calls *backupRoot*.

    Any cached iOSbackup instance for *udid* is evicted so the next
    ``get_iosbackup()`` call opens the freshly registered directory.
    """
    backup_root = backup_dir.parent
    if _backup_roots.get(udid) == backup_root:
        return  # already registered — nothing to do

    _backup_roots[udid] = backup_root

    # Evict stale cache entry so it is re-opened against the correct root
    if udid in _iosbackup_cache:
        try:
            _iosbackup_cache[udid].close()
        except Exception:
            pass
        del _iosbackup_cache[udid]
        logger.debug(
            "device_connection_cache: evicted iOSbackup for %s "
            "(new backupRoot: %s)",
            udid, backup_root,
        )


def get_backup_password(udid: str) -> str | None:
    """Return the registered backup password for *udid*, or None."""
    return _backup_passwords.get(udid)


def get_backup_dir(udid: str) -> Path | None:
    """
    Return the registered backup directory for *udid*, or None.

    The backup directory is ``backup_root / udid`` where backup_root was
    registered via :func:`register_backup_dir`.
    """
    backup_root = _backup_roots.get(udid)
    if backup_root is None:
        return None
    backup_dir = backup_root / udid
    if backup_dir.exists():
        return backup_dir
    return None


# ---------------------------------------------------------------------------
# iOSbackup
# ---------------------------------------------------------------------------

def get_iosbackup(udid: str, derivedkey: str | None = None) -> Any:
    """
    Return a cached ``iOSbackup`` instance for *udid*, creating one if needed.

    If a backup root has been registered via ``register_backup_dir()`` the
    instance is opened against that directory; otherwise the iOSbackup
    library falls back to the system iTunes/Finder backup location.

    Raises ``ImportError`` if the ``iOSbackup`` package is not installed.
    """
    if udid not in _iosbackup_cache:
        from iOSbackup import iOSbackup  # type: ignore[import]
        backup_root = _backup_roots.get(udid)
        password = _backup_passwords.get(udid)
        kwargs: dict[str, Any] = {"udid": udid, "derivedkey": derivedkey}
        if password and not derivedkey:
            kwargs["cleartextpassword"] = password
        if backup_root is not None:
            # iOSbackup kwarg is lowercase 'backuproot', not 'backupRoot'
            kwargs["backuproot"] = str(backup_root)
            logger.debug(
                "device_connection_cache: creating iOSbackup for %s "
                "(backuproot=%s)",
                udid, backup_root,
            )
        else:
            logger.debug(
                "device_connection_cache: creating iOSbackup for %s "
                "(using system default backup location)",
                udid,
            )
        _iosbackup_cache[udid] = iOSbackup(**kwargs)
    return _iosbackup_cache[udid]


# ---------------------------------------------------------------------------
# IOSServiceBroker
# ---------------------------------------------------------------------------

def get_broker(udid: str) -> Any:
    """
    Return a cached ``IOSServiceBroker`` instance for *udid*.

    The broker caches its own lockdown/AFC/AFC2 handles internally, so a
    single instance per UDID avoids repeated USB handshakes across extractors.
    """
    if udid not in _broker_cache:
        from core.ios_service_broker import IOSServiceBroker
        logger.debug("device_connection_cache: creating IOSServiceBroker for %s", udid)
        _broker_cache[udid] = IOSServiceBroker(udid=udid)
    return _broker_cache[udid]


def get_lockdown(udid: str) -> Any:
    """
    Return a live LockdownClient for *udid* via the cached broker.

    Uses the broker's ``get_lockdown()`` which tries ``create_using_usbmux``
    (pymobiledevice3 9.x) before falling back to ``LockdownClient(serial=)``
    and the legacy positional constructor.  This avoids the
    ``Can't instantiate abstract class LockdownClient`` error from pmd3 9.x.
    """
    return get_broker(udid).get_lockdown()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def clear_connection_cache() -> None:
    """
    Close and evict all cached connection objects.

    Call this at the end of a transfer session to release USB handles and
    remove temporary Manifest.db decrypt files.
    """
    for udid, backup in list(_iosbackup_cache.items()):
        try:
            backup.close()
        except Exception as exc:
            logger.debug("device_connection_cache: error closing iOSbackup for %s: %s", udid, exc)
    _iosbackup_cache.clear()

    for udid, broker in list(_broker_cache.items()):
        try:
            broker.close()
        except Exception as exc:
            logger.debug("device_connection_cache: error closing broker for %s: %s", udid, exc)
    _broker_cache.clear()

    _backup_roots.clear()
    _backup_passwords.clear()

    logger.debug("device_connection_cache: cleared")
