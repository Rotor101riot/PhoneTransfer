"""
core/vault_manager.py

Orchestrates Phone → PC backup and PC → Phone (cross-ecosystem) restore
using the vault ZIP format produced by vault_writer.py.

Two public entry points
-----------------------
backup_device(source, output_path, categories, on_progress)
    Extract all requested categories from *source* (iOS or Android) and
    write a vault ZIP to *output_path*.

restore_from_vault(vault_path, destination, categories, on_progress)
    Read a vault ZIP and inject each category into *destination* (iOS or
    Android) using the standard inject_{category}_{platform} modules.
    Cross-ecosystem conversion (iOS backup → Android, vice versa) is
    transparent — the vault format is platform-neutral.

Both functions return a summary dict identical in shape to the one
produced by PipelineManager.run() so the UI can reuse the same result
rendering logic.

Usage
-----
    from core.vault_manager import backup_device, restore_from_vault
    from core.normalization_schema import DeviceInfo
    from pathlib import Path

    # Phone → PC
    summary = backup_device(
        source=my_iphone,
        output_path=Path("~/Desktop/iphone_backup.zip"),
        categories=["contacts", "sms", "photos"],
        on_progress=lambda cat, done, total: print(f"{cat}: {done}/{total}"),
    )

    # PC → Phone (cross-ecosystem restore)
    summary = restore_from_vault(
        vault_path=Path("~/Desktop/iphone_backup.zip"),
        destination=my_android,
        categories=["contacts", "sms"],
        on_progress=lambda cat, done, total: print(f"{cat}: {done}/{total}"),
    )
"""

from __future__ import annotations

import importlib
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from core.normalization_schema import DeviceInfo
from core.vault_writer import VaultWriter
from core.vault_reader import VaultReader

logger = logging.getLogger(__name__)

# Lock protecting mutations of the shared Config singleton from worker threads.
_config_lock = threading.Lock()

# All categories the vault engine knows how to handle.
VAULT_CATEGORIES: list[str] = [
    "contacts", "sms", "calls", "calendar", "notes",
    "alarms", "reminders", "bookmarks", "blocked",
    "photos", "videos", "ringtones", "voice_memos", "wallpaper",
    "whatsapp",
    # "telegram",   # extractors/injectors exist but require Telegram auth flow — not yet wired
    "health", "browser",
    "apps",     # Android only: third-party APKs via adb pull
    # "signal"  # not yet implemented — requires explicit Signal backup export
]

ProgressCallback = Optional[Callable[[str, int, int], None]]


# ---------------------------------------------------------------------------
# Phone → PC backup
# ---------------------------------------------------------------------------

def backup_device(
    source: DeviceInfo,
    output_path: Path,
    categories: list[str] | None = None,
    on_progress: ProgressCallback = None,
    on_backup_progress: Optional[Callable[[float, str], None]] = None,
    on_password_needed: Optional[Callable[[], "str | None"]] = None,
    staging_dir: Path | None = None,
    since: datetime | None = None,
    encryption_password: str | None = None,
) -> dict:
    """
    Extract all *categories* from *source* and write a vault ZIP to
    *output_path*.

    Parameters
    ----------
    source:
        Connected source device (iOS or Android).
    output_path:
        Where to write the vault ZIP.  Parent directory must exist.
    categories:
        Subset of ``VAULT_CATEGORIES`` to include.  ``None`` → all.
    on_progress:
        Optional callback(category, done, total) called after each
        category finishes.
    staging_dir:
        Temporary directory for extractor intermediates.  Defaults to
        ``output_path.parent / ".pt_staging"``.
    since:
        If provided, items with timestamps older than this datetime are
        filtered out after extraction (incremental/delta backup).
    encryption_password:
        If provided, the vault ZIP is encrypted with AES-256-GCM using
        this passphrase and the ``.enc`` suffix is appended.

    Returns
    -------
    dict
        Summary with keys: ``vault_path``, ``source``, ``categories``
        (per-category status/count dicts).
    """
    cats = _validated_categories(categories)
    if staging_dir is None:
        staging_dir = output_path.parent / ".pt_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    # iOS source: run a full MobileSync backup before any extractor so that
    # iOSbackup reads from our directory instead of the system iTunes path.
    _backup_mgr = None
    if source.platform == "ios":
        try:
            from core.backup_manager_ios import BackupManager
            from core.config_loader import get_config
            _cfg = get_config()
            _backup_mgr = BackupManager(udid=source.serial, config=_cfg)
            logger.info("backup: ensuring iOS MobileSync backup for %s …", source.serial)
            if not _backup_mgr.ensure_backup_for_transfer(
                on_progress=on_backup_progress,
                on_password_needed=on_password_needed,
                # NOTE: encryption_password here is the vault ZIP password, not
                # the iOS device backup password — do not pass it to BackupManager.
            ):
                logger.error("backup: iOS backup failed — aborting vault creation")
                _err = Exception("iOS backup could not be obtained or decrypted")
                return {
                    "vault_path":  str(output_path),
                    "created_at":  datetime.now(timezone.utc).isoformat(),
                    "source":      {"platform": source.platform, "serial": source.serial, "name": source.name},
                    "categories":  {cat: _failed(_err) for cat in cats},
                }
        except Exception as exc:
            logger.error("backup: iOS backup phase raised: %s", exc)
            return {
                "vault_path":  str(output_path),
                "created_at":  datetime.now(timezone.utc).isoformat(),
                "source":      {"platform": source.platform, "serial": source.serial, "name": source.name},
                "categories":  {cat: _failed(exc) for cat in cats},
            }

    # If encrypting, write the plain ZIP to a temp file first
    if encryption_password:
        plain_path = output_path.with_suffix(output_path.suffix + ".plain_tmp")
    else:
        plain_path = output_path

    results: dict[str, dict] = {}

    # For Wi-Fi Android devices, route all extraction through WifiAndroidExtractor
    # instead of importing ADB-based extract_{category}_android modules.
    wifi_extractor = None
    if getattr(source, "transport", "usb") == "wifi":
        try:
            from core.wifi_discovery import WifiCompanionSession, CompanionDevice
            from core.wifi_android_extractor import WifiAndroidExtractor
            _cd = CompanionDevice(
                name=source.name,
                host=source.wifi_host or source.serial,
                port=getattr(source, "wifi_port", 7337),
                properties={},
            )
            _session = WifiCompanionSession(_cd)
            _session.connect()
            wifi_extractor = WifiAndroidExtractor(_session)
            logger.info("backup: using Wi-Fi transport for %s @ %s", source.name, _cd.host)
        except Exception as exc:
            logger.error("backup: could not open Wi-Fi session: %s — falling back to ADB", exc)

    # Grab cancel_event once so we can check it between categories.
    _cancel_event = None
    try:
        from core.config_loader import get_config as _get_config
        _cancel_event = _get_config().cancel_event
    except Exception:
        pass

    with VaultWriter(plain_path, source_device=source) as writer:
        for i, category in enumerate(cats):
            # Honour cancellation between categories so the UI stays responsive.
            if _cancel_event is not None and _cancel_event.is_set():
                logger.info("backup: cancelled by user before %s — stopping.", category)
                break

            cat_staging = staging_dir / category
            cat_staging.mkdir(parents=True, exist_ok=True)

            try:
                if wifi_extractor is not None:
                    items = wifi_extractor.extract(category, cat_staging)
                else:
                    extractor = _load_extractor(category, source.platform)
                    if extractor is None:
                        results[category] = _skipped(f"no extractor for {category}/{source.platform}")
                        logger.info("backup: skipping %s (no extractor)", category)
                        continue
                    items = extractor(source.serial, cat_staging, source.is_jailbroken or source.is_rooted)

                if since is not None:
                    items = _filter_since(items, since)
                n = writer.add_category(category, items)
                results[category] = {"status": "completed", "extracted": n, "written": n, "error": None}
                logger.info("backup: %s → %d items", category, n)
            except Exception as exc:
                results[category] = _failed(exc)
                logger.error("backup: %s failed: %s", category, exc)

            if on_progress:
                on_progress(category, i + 1, len(cats))

    if wifi_extractor is not None:
        try:
            wifi_extractor._session.disconnect()
        except Exception:
            pass

    # Encrypt if requested
    final_path = plain_path
    if encryption_password:
        try:
            from core.vault_crypto import encrypt_vault
            final_path = encrypt_vault(plain_path, encryption_password, output_path)
        except Exception as exc:
            logger.error("backup: encryption failed: %s", exc)
            # Leave the plain vault in place rather than losing the backup
            final_path = plain_path

    # Optionally delete the iOS backup now that all extractors are done.
    if _backup_mgr is not None:
        try:
            from core.settings_manager import get_settings as _get_settings
            if _get_settings().ios_delete_backup_after_extract:
                _backup_mgr.delete_backup_if_safe()
        except Exception as exc:
            logger.warning("backup: post-extract cleanup failed (non-fatal): %s", exc)

    return {
        "vault_path": str(final_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source":     {"platform": source.platform, "serial": source.serial, "name": source.name},
        "categories": results,
    }


# ---------------------------------------------------------------------------
# Incremental backup — only items newer than a previous vault
# ---------------------------------------------------------------------------

def incremental_backup_device(
    source: DeviceInfo,
    output_path: Path,
    previous_vault: Path,
    categories: list[str] | None = None,
    on_progress: ProgressCallback = None,
    staging_dir: Path | None = None,
    encryption_password: str | None = None,
) -> dict:
    """
    Back up only items that are newer than the most recent vault in
    *previous_vault*.

    Reads the ``created_at`` timestamp from *previous_vault*'s manifest
    and passes it as ``since`` to :func:`backup_device`.  Items without
    timestamps (Alarms, Bookmarks, etc.) are always included.

    Parameters
    ----------
    source:
        Source device.
    output_path:
        Destination for the new (incremental) vault ZIP.
    previous_vault:
        Path to an existing vault ZIP whose ``created_at`` sets the cutoff.
    categories, on_progress, staging_dir, encryption_password:
        Forwarded to :func:`backup_device`.
    """
    since: datetime | None = None
    try:
        with VaultReader(previous_vault) as reader:
            created_at_str = reader.manifest.get("created_at", "")
            if created_at_str:
                since = datetime.fromisoformat(created_at_str.rstrip("Z"))
                if since.tzinfo is None:
                    since = since.replace(tzinfo=timezone.utc)
        if since:
            logger.info(
                "incremental_backup: cutoff timestamp = %s (from %s)",
                since.isoformat(), previous_vault.name,
            )
    except Exception as exc:
        logger.warning(
            "incremental_backup: could not read previous vault '%s': %s — "
            "falling back to full backup",
            previous_vault, exc,
        )

    return backup_device(
        source=source,
        output_path=output_path,
        categories=categories,
        on_progress=on_progress,
        staging_dir=staging_dir,
        since=since,
        encryption_password=encryption_password,
    )


# ---------------------------------------------------------------------------
# PC → Phone restore (cross-ecosystem)
# ---------------------------------------------------------------------------

def restore_from_vault(
    vault_path: Path,
    destination: DeviceInfo,
    categories: list[str] | None = None,
    on_progress: ProgressCallback = None,
    staging_dir: Path | None = None,
) -> dict:
    """
    Read a vault ZIP and inject each category into *destination*.

    The source platform encoded in the vault is irrelevant — the vault
    format is platform-neutral, so an iOS backup restores cleanly to
    Android and vice versa.

    Parameters
    ----------
    vault_path:
        Path to the vault ZIP created by :func:`backup_device`.
    destination:
        Connected destination device (iOS or Android).
    categories:
        Subset of categories to restore.  ``None`` → all available.
    on_progress:
        Optional callback(category, done, total) called after each
        category finishes.
    staging_dir:
        Temporary directory for injector intermediates.

    Returns
    -------
    dict
        Summary with keys: ``vault_path``, ``destination``,
        ``categories`` (per-category status/count dicts).
    """
    if staging_dir is None:
        staging_dir = vault_path.parent / ".pt_staging_restore"
    staging_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, dict] = {}

    # Wi-Fi injection path for Android destinations
    wifi_extractor = None
    if getattr(destination, "transport", "usb") == "wifi":
        try:
            from core.wifi_discovery import WifiCompanionSession, CompanionDevice
            from core.wifi_android_extractor import WifiAndroidExtractor
            _cd = CompanionDevice(
                name=destination.name,
                host=destination.wifi_host or destination.serial,
                port=getattr(destination, "wifi_port", 7337),
                properties={},
            )
            _session = WifiCompanionSession(_cd)
            _session.connect()
            wifi_extractor = WifiAndroidExtractor(_session)
            logger.info("restore: using Wi-Fi transport for %s @ %s", destination.name, _cd.host)
        except Exception as exc:
            logger.error("restore: could not open Wi-Fi session: %s — falling back to ADB", exc)

    with VaultReader(vault_path) as reader:
        manifest  = reader.manifest
        available = reader.available_categories

        if categories is None:
            cats = available
        else:
            cats = [c for c in _validated_categories(categories) if c in available]

        logger.info(
            "restore: vault source=%s, destination=%s, categories=%s",
            manifest.get("source_platform", "?"),
            destination.platform,
            cats,
        )

        for i, category in enumerate(cats):
            cat_staging = staging_dir / category
            cat_staging.mkdir(parents=True, exist_ok=True)

            try:
                items = reader.load_category(category)
                if not items:
                    results[category] = _skipped("no items in vault for this category")
                    continue

                if wifi_extractor is not None:
                    injected = wifi_extractor.inject(category, items, cat_staging)
                else:
                    injector = _load_injector(category, destination.platform)
                    if injector is None:
                        results[category] = _skipped(f"no injector for {category}/{destination.platform}")
                        continue
                    injected = injector(destination.serial, items, cat_staging, destination.is_jailbroken or destination.is_rooted)

                results[category] = {"status": "completed", "loaded": len(items), "injected": injected, "error": None}
                logger.info("restore: %s → %d/%d injected", category, injected, len(items))
            except Exception as exc:
                results[category] = _failed(exc)
                logger.error("restore: %s failed: %s", category, exc)

            if on_progress:
                on_progress(category, i + 1, len(cats))

    if wifi_extractor is not None:
        try:
            wifi_extractor._session.disconnect()
        except Exception:
            pass

    return {
        "vault_path":  str(vault_path),
        "destination": {"platform": destination.platform, "serial": destination.serial, "name": destination.name},
        "vault_source": manifest.get("source_platform", "unknown"),
        "categories":  results,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validated_categories(cats: list[str] | None) -> list[str]:
    if cats is None:
        return list(VAULT_CATEGORIES)
    unknown = [c for c in cats if c not in VAULT_CATEGORIES]
    if unknown:
        logger.warning("vault_manager: unknown categories ignored: %s", unknown)
    return [c for c in cats if c in VAULT_CATEGORIES]


def _load_extractor(category: str, platform: str) -> Callable | None:
    return _load_fn(f"core.extract_{category}_{platform}", "extract", "extractor", category, platform)


def _load_injector(category: str, platform: str) -> Callable | None:
    return _load_fn(f"core.inject_{category}_{platform}", "inject", "injector", category, platform)


def _load_fn(module: str, attr: str, role: str, category: str, platform: str) -> Callable | None:
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError:
        logger.debug("vault_manager: no %s for %s/%s", role, category, platform)
        return None
    except ImportError as exc:
        logger.warning("vault_manager: import error for %s: %s", module, exc)
        return None
    fn = getattr(mod, attr, None)
    if fn is None or not callable(fn):
        return None
    return fn


def _filter_since(items: list, since: datetime) -> list:
    """
    Return only items whose primary timestamp field is >= *since*.

    Items without a detectable timestamp are always included so that
    category types without timestamps (Alarm, Bookmark, etc.) are never
    silently dropped during an incremental backup.
    """
    result = []
    for item in items:
        ts = getattr(item, "timestamp", None) or getattr(item, "created_at", None)
        if ts is None:
            result.append(item)
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= since:
            result.append(item)
    return result


def _skipped(reason: str) -> dict:
    return {"status": "skipped", "extracted": 0, "injected": 0, "error": reason}


def _failed(exc: Exception) -> dict:
    return {"status": "failed", "extracted": 0, "injected": 0, "error": str(exc)}
