"""
pipeline_manager.py

Routes a (source_platform, dest_platform) transfer pair to the correct
set of extractor and injector callables, then drives SessionManager
through each requested category.

Extractor/injector modules follow the naming convention::

    core.extract_{category}_{platform}   — exports ``extract(staging_path: Path) -> list``
    core.inject_{category}_{platform}    — exports ``inject(items: list, staging_path: Path) -> int``

For example, transferring contacts from iOS to Android uses:
    - ``core.extract_contacts_ios.extract``
    - ``core.inject_contacts_android.inject``

If a module cannot be imported (not yet implemented, missing dependency,
etc.) a warning is logged and that category is skipped gracefully.

Typical usage::

    from core.normalization_schema import DeviceInfo
    from core.pipeline_manager import PipelineManager

    mgr = PipelineManager(source_device, destination_device, categories=["contacts", "sms"])
    summary = mgr.run()
    print(summary)
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import os
import queue as _queue
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from core.adb_manager import ADBManager
from core.config_loader import Config, get_config
from core.normalization_schema import DeviceInfo
from core.session_manager import ALL_CATEGORIES, SessionManager

if TYPE_CHECKING:
    from core.wifi_android_extractor import WifiAndroidExtractor

logger = logging.getLogger(__name__)


def _open_wifi_extractor(dev: DeviceInfo) -> "WifiAndroidExtractor | None":
    """
    Open a Wi-Fi companion session for *dev* and return a WifiAndroidExtractor.
    Returns None if the connection fails.
    """
    try:
        from core.wifi_discovery import WifiCompanionSession, CompanionDevice
        from core.wifi_android_extractor import WifiAndroidExtractor
        cd = CompanionDevice(
            name       = dev.name,
            host       = dev.wifi_host or dev.serial,
            port       = getattr(dev, "wifi_port", 7337),
            properties = {},
        )
        session = WifiCompanionSession(cd)
        session.connect()
        logger.info("pipeline: Wi-Fi session opened for %s @ %s", dev.name, cd.host)
        return WifiAndroidExtractor(session)
    except Exception as exc:
        logger.error("pipeline: could not open Wi-Fi session for %s: %s", dev.name, exc)
        return None

# ---------------------------------------------------------------------------
# Companion app status notification
# ---------------------------------------------------------------------------

_COMPANION_PKG = "com.phonetransfer.companion"
_COMPANION_ACTION = "com.phonetransfer.STATUS"


def _notify_companion(
    adb: ADBManager,
    dest_serial: str,
    category: str | None,
    done: int = 0,
    total: int = 0,
) -> None:
    """
    Fire an ADB broadcast to the companion app on *dest_serial* so its status
    dot updates during a transfer that uses ADB directly (rather than the
    companion's TCP socket).

    The companion's ``AdbStatusReceiver`` picks this up and relays it as a
    ``LocalBroadcast`` to ``MainActivity``, which transitions the dot from
    amber "Waiting for PC" → blue "Transferring…" (and back on completion).

    This is a best-effort fire-and-forget call; failures are silently ignored
    so they never abort the transfer.

    Parameters
    ----------
    category:
        Human-readable category label shown in the companion UI (e.g. "Contacts").
        Pass ``None`` or leave *total* at 0 to signal that no transfer is active.
    done / total:
        Progress counters.  ``total == 0`` clears the TRANSFERRING state.
    """
    if category and total > 0:
        cmd = (
            f"am broadcast -p {_COMPANION_PKG} -a {_COMPANION_ACTION} "
            f"--es category '{category}' --ei done {done} --ei total {total}"
        )
    else:
        cmd = (
            f"am broadcast -p {_COMPANION_PKG} -a {_COMPANION_ACTION} "
            "--ei done 0 --ei total 0"
        )
    try:
        adb.shell(dest_serial, cmd, timeout=5)
    except Exception as exc:
        logger.debug("_notify_companion: broadcast failed (non-fatal): %s", exc)

def _hardlink_backup(src: Path, dst: Path) -> int:
    """
    Hardlink every file in *src* into *dst*, preserving subdirectory structure.
    Falls back to shutil.copy2 if the filesystem doesn't support hardlinks
    (e.g. cross-volume).  Returns the number of files copied.
    """
    import shutil
    dst.mkdir(parents=True, exist_ok=True)
    count = 0
    for item in src.rglob("*"):
        if item.is_file():
            target = dst / item.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(item, target)
            except OSError:
                shutil.copy2(item, target)
            count += 1
    return count


_ACTIVE_PIPELINES: set[tuple[str, str]] = set()
_ACTIVE_PIPELINES_MUTEX = threading.Lock()


@contextlib.contextmanager
def _pipeline_lock(lock_path: Path, src_id: str, dst_id: str):
    """
    Context manager that prevents concurrent pipeline runs for the same
    device pair.  Uses both an in-process threading set (catches two threads)
    and a PID lockfile (catches two desktop app instances).

    Raises RuntimeError if the lock cannot be acquired.
    """
    pair = (src_id, dst_id)
    with _ACTIVE_PIPELINES_MUTEX:
        if pair in _ACTIVE_PIPELINES:
            raise RuntimeError(
                f"Transfer already in progress for {src_id} → {dst_id} "
                "(same process). Wait for it to finish."
            )
        _ACTIVE_PIPELINES.add(pair)
    try:
        # Cross-process lockfile using O_EXCL for atomic creation
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY | os.O_EXCL)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            # Check if the owning PID is still alive (stale lock detection)
            try:
                with open(lock_path) as f:
                    existing_pid = int(f.read().strip())
                os.kill(existing_pid, 0)
                # Process is alive — refuse
                raise RuntimeError(
                    f"Transfer already in progress for {src_id} → {dst_id} "
                    f"(PID {existing_pid}). Wait for it to finish."
                )
            except (ValueError, OSError):
                # Stale lock from a crashed instance — overwrite
                try:
                    os.unlink(str(lock_path))
                    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY | os.O_EXCL)
                    os.write(fd, str(os.getpid()).encode())
                    os.close(fd)
                except OSError:
                    pass  # lockfile is best-effort; in-process lock is primary
        yield
    finally:
        with _ACTIVE_PIPELINES_MUTEX:
            _ACTIVE_PIPELINES.discard(pair)
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass


def _check_ios_version_compatibility(
    source: "DeviceInfo", destination: "DeviceInfo"
) -> None:
    """
    Log a pre-flight warning when the source iOS version is newer than the
    destination iOS version.

    Schema drift across major iOS releases is real — ABMultiValue.uid presence,
    ZREMCDREMINDER column changes, MTAlarmDataVersion bumps, etc.  Injectors
    use PRAGMA table_info tolerance paths to survive these differences, but they
    may silently produce empty or partial results.  Surfacing a warning before
    the transfer starts lets the user make an informed decision.
    """
    def _major(version_str: str) -> int | None:
        try:
            return int(version_str.split(".")[0])
        except (ValueError, IndexError, AttributeError):
            return None

    src_major = _major(source.os_version)
    dst_major = _major(destination.os_version)

    if src_major is None or dst_major is None:
        return  # can't compare — silently skip

    if source.platform == "ios" and destination.platform == "ios":
        if src_major > dst_major:
            logger.warning(
                "pre-flight: source is iOS %s (v%d) but destination is iOS %s (v%d). "
                "Injectors will attempt schema tolerance, but some categories may "
                "produce empty or partial results on the older destination. "
                "Upgrading the destination device to iOS %d+ before transferring "
                "is strongly recommended.",
                source.os_version, src_major,
                destination.os_version, dst_major,
                src_major,
            )
        elif src_major == dst_major - 1:
            logger.info(
                "pre-flight: source iOS %s is one major version behind destination "
                "iOS %s — schema compatibility expected but unverified on this pair.",
                source.os_version, destination.os_version,
            )
    elif source.platform == "ios" and destination.platform == "android":
        if src_major and src_major >= 18:
            logger.info(
                "pre-flight: source is iOS %s (v%d ≥ 18). "
                "iOS 18 tightened backup encryption schemas in some areas; "
                "verify extractor output if contacts or health data appear empty.",
                source.os_version, src_major,
            )


# Supported platform identifiers.
_PLATFORMS = frozenset({"ios", "android"})

# Ordered default category list (subset of session_manager.ALL_CATEGORIES).
_DEFAULT_CATEGORIES: list[str] = list(ALL_CATEGORIES)


class PipelineManager:
    """
    Discovers available extractors and injectors for a given transfer
    scenario (ios→ios, ios→android, android→ios, android→android),
    then drives SessionManager through each category.

    Parameters
    ----------
    source:
        DeviceInfo for the source device.  ``source.platform`` must be
        ``"ios"`` or ``"android"``.
    destination:
        DeviceInfo for the destination device.  ``destination.platform``
        must be ``"ios"`` or ``"android"``.
    categories:
        Explicit list of category names to transfer.  Pass ``None`` (the
        default) to attempt all categories defined in
        ``session_manager.ALL_CATEGORIES``.
    config:
        Pre-built Config instance.  If ``None``, ``get_config()`` is
        called lazily on first use.

    Raises
    ------
    ValueError
        If either platform string is not ``"ios"`` or ``"android"``.
    """

    def __init__(
        self,
        source: DeviceInfo,
        destination: DeviceInfo,
        categories: list[str] | None = None,
        config: Config | None = None,
        dry_run: bool = False,
        resume_session_id: str | None = None,
    ) -> None:
        if source.platform not in _PLATFORMS:
            raise ValueError(
                f"Unsupported source platform: {source.platform!r}. "
                f"Expected one of {sorted(_PLATFORMS)}."
            )
        if destination.platform not in _PLATFORMS:
            raise ValueError(
                f"Unsupported destination platform: {destination.platform!r}. "
                f"Expected one of {sorted(_PLATFORMS)}."
            )

        self.source = source
        self.destination = destination
        self.categories: list[str] = (
            categories if categories is not None else list(_DEFAULT_CATEGORIES)
        )
        self._config: Config | None = config
        self.dry_run: bool = dry_run
        self.resume_session_id: str | None = resume_session_id
        self._backup_progress_cb: Callable[[float, str], None] | None = None

    # ------------------------------------------------------------------
    # Config (lazy)
    # ------------------------------------------------------------------

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = get_config()
        return self._config

    # ------------------------------------------------------------------
    # Resume helpers
    # ------------------------------------------------------------------

    @classmethod
    def find_resumable_session(
        cls,
        source: "DeviceInfo",
        dest: "DeviceInfo",
        config: "Config | None" = None,
    ) -> "str | None":
        """
        Scan ``config.temp_dir`` for an incomplete session matching this
        device pair.

        Returns the ``session_id`` string of the most-recently-updated
        resumable session, or ``None`` if none is found.

        A session is resumable when its ``session.json`` exists,
        ``completed`` is False, ``aborted`` is False, and the
        source/dest serials match.
        """
        from core.config_loader import get_config as _gc
        cfg = config or _gc()
        temp_dir = cfg.temp_dir
        if not temp_dir.is_dir():
            return None

        from core import session_file as _sf
        best: tuple[str, str] | None = None  # (updated_at, session_id)

        for candidate in temp_dir.iterdir():
            if not candidate.is_dir():
                continue
            session = _sf.load(str(candidate))
            if session is None:
                continue
            if session.get("completed") or session.get("aborted"):
                continue
            if (
                session.get("source_serial") != source.serial
                or session.get("dest_serial") != dest.serial
            ):
                continue
            updated = session.get("updated_at", "")
            if best is None or updated > best[0]:
                best = (updated, session["session_id"])

        return best[1] if best else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def preflight_scan(self) -> dict:
        """
        Quick pre-transfer scan that estimates item counts and sizes per
        category on the source device WITHOUT extracting data.

        Returns
        -------
        dict
            Structure::

                {
                    "categories": {
                        "<category>": {"count": int, "estimated_bytes": int},
                        ...
                    },
                    "total_items": int,
                    "total_bytes": int,
                    "dest_free_bytes": int | None,
                }
        """
        results: dict[str, dict] = {}
        adb: ADBManager | None = None

        if self.source.platform == "android":
            try:
                adb = ADBManager(self.config)
            except Exception:
                pass

        _content_uri_map: dict[str, str] = {
            "contacts":       "content://com.android.contacts/contacts",
            "sms":            "content://sms",
            "calls":          "content://call_log/calls",
            "calendar":       "content://com.android.calendar/events",
            "bookmarks":      "content://browser/bookmarks",
        }

        _media_dirs: dict[str, str] = {
            "photos":     "/sdcard/DCIM",
            "videos":     "/sdcard/DCIM",
            "ringtones":  "/sdcard/Ringtones",
            "voice_memos": "/sdcard/Recordings",
        }

        for category in self.categories:
            count = 0
            est_bytes = 0

            try:
                if self.source.platform == "android" and adb is not None:
                    serial = self.source.serial

                    if category in _content_uri_map:
                        uri = _content_uri_map[category]
                        stdout, _, rc = adb.shell(
                            serial,
                            f"content query --uri {uri} --projection _id 2>/dev/null | wc -l",
                            timeout=10,
                        )
                        if rc == 0 and stdout.strip().isdigit():
                            count = int(stdout.strip())
                            # Rough per-record size estimates
                            per_record = {"contacts": 2048, "sms": 512,
                                          "calls": 256, "calendar": 1024,
                                          "bookmarks": 512}.get(category, 512)
                            est_bytes = count * per_record

                    elif category in _media_dirs:
                        media_dir = _media_dirs[category]
                        ext_filter = {
                            "photos": r"-iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.heic'",
                            "videos": r"-iname '*.mp4' -o -iname '*.mov' -o -iname '*.mkv' -o -iname '*.3gp'",
                            "ringtones": r"-iname '*.mp3' -o -iname '*.ogg' -o -iname '*.m4a'",
                            "voice_memos": r"-iname '*.m4a' -o -iname '*.3gp' -o -iname '*.amr'",
                        }.get(category, "")
                        stdout, _, rc = adb.shell(
                            serial,
                            f"find {media_dir} -type f \\( {ext_filter} \\) 2>/dev/null | wc -l",
                            timeout=15,
                        )
                        if rc == 0 and stdout.strip().isdigit():
                            count = int(stdout.strip())
                        # Estimate average sizes
                        avg_size = {"photos": 3_500_000, "videos": 50_000_000,
                                    "ringtones": 500_000, "voice_memos": 1_000_000,
                                    }.get(category, 1_000_000)
                        est_bytes = count * avg_size

                    elif category == "whatsapp":
                        stdout, _, rc = adb.shell(
                            serial,
                            "ls /sdcard/Android/media/com.whatsapp/WhatsApp/Databases/msgstore*.db.crypt* 2>/dev/null | wc -l",
                            timeout=10,
                        )
                        if rc == 0 and stdout.strip().isdigit():
                            n = int(stdout.strip())
                            count = 1 if n > 0 else 0
                        # Estimate WhatsApp total (DB + media)
                        stdout2, _, rc2 = adb.shell(
                            serial,
                            "du -sb /sdcard/Android/media/com.whatsapp/WhatsApp/ 2>/dev/null | cut -f1",
                            timeout=15,
                        )
                        if rc2 == 0 and stdout2.strip().isdigit():
                            est_bytes = int(stdout2.strip())

                    elif category == "installed_apps":
                        stdout, _, rc = adb.shell(
                            serial,
                            "pm list packages -3 2>/dev/null | wc -l",
                            timeout=10,
                        )
                        if rc == 0 and stdout.strip().isdigit():
                            count = int(stdout.strip())
                            est_bytes = count * 30_000_000  # ~30MB avg APK

                elif self.source.platform == "ios":
                    # iOS: estimate from backup manifest if available
                    try:
                        from core.backup_parser_ios import BackupParser
                        parser = BackupParser(self.source.serial, self._config or get_config())
                        manifest = parser.get_manifest_entries()

                        _ios_domain_map: dict[str, list[str]] = {
                            "contacts": ["HomeDomain-Library/AddressBook"],
                            "sms": ["HomeDomain-Library/SMS"],
                            "photos": ["CameraRollDomain", "MediaDomain"],
                            "calendar": ["HomeDomain-Library/Calendar"],
                            "notes": ["HomeDomain-Library/Notes"],
                            "whatsapp": ["AppDomainGroup-group.net.whatsapp.WhatsApp"],
                            "voice_memos": ["MediaDomain-Recordings"],
                        }
                        domains = _ios_domain_map.get(category, [])
                        for entry in manifest:
                            domain = entry.get("domain", "")
                            if any(domain.startswith(d) for d in domains):
                                count += 1
                                est_bytes += entry.get("size", 0)
                    except Exception:
                        pass  # No backup available yet — counts will be 0

            except Exception as exc:
                logger.debug("preflight_scan: %s failed: %s", category, exc)

            results[category] = {"count": count, "estimated_bytes": est_bytes}

        total_items = sum(v["count"] for v in results.values())
        total_bytes = sum(v["estimated_bytes"] for v in results.values())

        # Check destination free space
        dest_free: int | None = None
        try:
            if self.destination.platform == "android":
                dest_adb = adb or ADBManager(self.config)
                stdout, _, rc = dest_adb.shell(
                    self.destination.serial,
                    "df /data | tail -1 | awk '{print $4}'",
                    timeout=10,
                )
                if rc == 0 and stdout.strip():
                    raw = stdout.strip().upper().replace("K", "").replace("M", "").replace("G", "")
                    if raw.isdigit():
                        # df output in 1K blocks
                        dest_free = int(raw) * 1024
            elif self.destination.platform == "ios":
                from core.device_connection_cache import get_lockdown_client
                ld = get_lockdown_client(self.destination.serial)
                disk_info = ld.get_value(domain="com.apple.disk_usage")
                if disk_info:
                    dest_free = disk_info.get("AmountDataAvailable", None)
        except Exception as exc:
            logger.debug("preflight_scan: free space check failed: %s", exc)

        return {
            "categories": results,
            "total_items": total_items,
            "total_bytes": total_bytes,
            "dest_free_bytes": dest_free,
        }

    def run(self) -> dict:
        """
        Execute the full transfer pipeline.

        For each requested category:
        1. Attempt to import the matching extractor and injector modules.
        2. Skip the category (with a warning) if either module is absent.
        3. Delegate execution to ``SessionManager.run_category``.
        4. Collect per-category results into a summary dict.

        Returns
        -------
        dict
            A summary with the following structure::

                {
                    "session_id": str | None,
                    "source": {"platform": str, "serial": str},
                    "destination": {"platform": str, "serial": str},
                    "categories": {
                        "<category>": {
                            "status": "completed" | "failed" | "skipped",
                            "extracted": int,
                            "injected": int,
                            "error": str | None,
                        },
                        ...
                    },
                }
        """
        # Build the list of runnable categories before opening the session so
        # we can pass only the relevant ones to SessionManager (which in turn
        # registers them in the session file).
        runnable: list[tuple[str, Callable, Callable]] = []
        skipped: list[str] = []

        for category in self.categories:
            extractor = self._get_extractor(category, self.source.platform)
            injector = self._get_injector(category, self.destination.platform)

            if extractor is None or injector is None:
                missing = []
                if extractor is None:
                    missing.append(f"extract_{category}_{self.source.platform}")
                if injector is None:
                    missing.append(f"inject_{category}_{self.destination.platform}")
                logger.warning(
                    "Skipping category '%s': module(s) not available: %s",
                    category,
                    ", ".join(missing),
                )
                skipped.append(category)
                continue

            runnable.append((category, extractor, injector))

        runnable_names = [cat for cat, _, _ in runnable]
        logger.info(
            "Pipeline %s→%s: %d categories to run, %d skipped.",
            self.source.platform,
            self.destination.platform,
            len(runnable),
            len(skipped),
        )

        # Accumulate results for the final summary.
        results: dict[str, dict] = {}

        # Pre-populate skipped entries.
        for cat in skipped:
            results[cat] = {
                "status": "skipped",
                "extracted": 0,
                "injected": 0,
                "error": None,
            }

        # When resuming, skip categories already completed in the prior session.
        if self.resume_session_id is not None:
            from core import session_file as _sf_resume
            _resume_cfg = self._config or get_config()
            _resume_staging = str(_resume_cfg.temp_dir / self.resume_session_id)
            _already_done = {
                cat for cat, state in (
                    _sf_resume.load(_resume_staging) or {}
                ).get("categories", {}).items()
                if state.get("status") == "completed"
            }
            if _already_done:
                logger.info(
                    "pipeline: resuming session %s — skipping %d already-completed "
                    "categories: %s",
                    self.resume_session_id,
                    len(_already_done),
                    ", ".join(sorted(_already_done)),
                )
                runnable = [(c, e, i) for c, e, i in runnable if c not in _already_done]
                runnable_names = [c for c, _, _ in runnable]
                for cat in _already_done:
                    if cat not in results:
                        results[cat] = {
                            "status": "completed",
                            "extracted": 0,
                            "injected": 0,
                            "error": None,
                            "resumed": True,
                        }

        session_manager = SessionManager(
            source=self.source,
            destination=self.destination,
            categories=runnable_names,
            config=self._config,
            existing_session_id=self.resume_session_id,
        )

        # Resolve device identifiers and privilege flags once before the loop.
        # iOS uses udid (== serial); Android uses ADB serial.
        # is_jailbroken / is_rooted map to the same privilege slot.
        src_id         = self.source.serial
        dst_id         = self.destination.serial
        src_privileged = self.source.is_jailbroken or self.source.is_rooted
        dst_privileged = self.destination.is_jailbroken or self.destination.is_rooted

        # Warn early if iOS version skew may cause silent schema failures.
        _check_ios_version_compatibility(self.source, self.destination)

        # Open Wi-Fi sessions for any device that is transport=="wifi".
        # These bypass the ADB module path entirely and route through the
        # companion TCP protocol instead.
        wifi_src: "WifiAndroidExtractor | None" = None
        wifi_dst: "WifiAndroidExtractor | None" = None

        if getattr(self.source, "transport", "usb") == "wifi":
            wifi_src = _open_wifi_extractor(self.source)
            if wifi_src is None:
                logger.error(
                    "pipeline: Wi-Fi source session failed to open — "
                    "extraction will be skipped for all categories"
                )

        if getattr(self.destination, "transport", "usb") == "wifi":
            wifi_dst = _open_wifi_extractor(self.destination)
            if wifi_dst is None:
                logger.error(
                    "pipeline: Wi-Fi destination session failed to open — "
                    "injection will be skipped for all categories"
                )

        # Parallel transfer pool for Wi-Fi companion-based file pushes.
        # Opens multiple TCP connections to the companion app so media files
        # can be transferred concurrently (like Dr.Fone's parallel streams).
        _transfer_pool = None
        if wifi_dst is not None:
            try:
                from core.transfer_pool import TransferPool
                dst_host = getattr(self.destination, "wifi_host", self.destination.serial)
                dst_port = getattr(self.destination, "wifi_port", 7337)
                _transfer_pool = TransferPool(
                    host=dst_host, port=dst_port, max_workers=4,
                )
                _transfer_pool.open()
                logger.info(
                    "pipeline: parallel transfer pool opened with %d connections",
                    len(_transfer_pool._clients),
                )
            except Exception as exc:
                logger.warning(
                    "pipeline: could not open transfer pool (non-fatal, "
                    "falling back to serial): %s", exc
                )
                _transfer_pool = None

        # ADB notifier used to update the companion app icon during transfers.
        # Only meaningful when the destination is a USB-connected Android device.
        _adb: ADBManager | None = None
        if self.destination.platform == "android" and wifi_dst is None:
            try:
                _adb = ADBManager(self.config)
            except Exception:
                pass  # non-fatal if config unavailable

        # iOS source: ensure a local MobileSync backup exists and is decrypted
        # before running any extractor.  Also registers the backup directory
        # with device_connection_cache so iOSbackup reads from the right path.
        _backup_mgr = None
        if self.source.platform == "ios":
            from core.backup_manager_ios import BackupManager
            _ios_cfg = self._config or get_config()
            _backup_mgr = BackupManager(udid=src_id, config=_ios_cfg)
            logger.info("pipeline: ensuring iOS backup for %s ...", src_id)
            if not _backup_mgr.ensure_backup_for_transfer(
                on_progress=getattr(self, "_backup_progress_cb", None),
                on_password_needed=getattr(self, "_password_needed_cb", None),
            ):
                logger.error(
                    "pipeline: iOS backup unavailable — aborting, "
                    "marking all categories failed"
                )
                for cat in runnable_names:
                    results[cat] = {
                        "status": "failed",
                        "extracted": 0,
                        "injected": 0,
                        "error": "iOS backup could not be obtained or decrypted",
                    }
                return {
                    "session_id": None,
                    "source": {
                        "platform": self.source.platform,
                        "serial": self.source.serial,
                    },
                    "destination": {
                        "platform": self.destination.platform,
                        "serial": self.destination.serial,
                    },
                    "categories": results,
                }

        # iOS destination: prepare a backup-modification injector so each
        # inject_*_ios module can write directly into the destination
        # device's encrypted backup.  All staged DB overrides are committed
        # in one re-pack pass after every category has finished injecting.
        _dest_injector_ctx: contextlib.AbstractContextManager = contextlib.nullcontext()
        # Per-category baselines captured immediately before each iOS
        # injector runs.  Consumed after commit by ios_backup_verify to
        # prove each category's row count actually grew in the repack.
        _dest_baselines: dict = {}
        # Log handler that records any inject_*_ios module silently
        # degrading from backup-mod to its AFC fallback.  In production
        # we treat such fallbacks as a hard failure (silent data loss).
        _fallback_detector = None
        if self.destination.platform == "ios":
            _dest_injector_ctx = self._open_dest_ios_backup_injector(dst_id)
            from core.ios_backup_verify import FallbackDetector
            _fallback_detector = FallbackDetector()
            logging.getLogger().addHandler(_fallback_detector)

        # Collects extracted items per category so they can be packed into a
        # universal archive after the pipeline finishes (iOS and Android).
        _archive_items: dict[str, list] = {}

        # Content dedup store — filters out items already transferred in
        # prior sessions between this device pair.
        try:
            from core.content_dedup import DedupStore
            _dedup = DedupStore(src_serial=src_id, dst_serial=dst_id)
        except Exception:
            _dedup = None

        # Categories that share a USB/device resource and must not be
        # extracted concurrently with each other.  Everything else is
        # safe to parallelise (e.g. backup-based iOS extractors all read
        # from a local SQLite copy of Manifest.db).
        _USB_BOUND_CATEGORIES = frozenset({
            "photos", "videos", "ringtones", "voice_memos", "wallpaper",
        })

        # Decide max parallelism — backup-based categories can fan out
        # aggressively; USB-bound categories stay sequential.
        _MAX_EXTRACT_WORKERS = 4

        # Install a SIGINT handler for the transfer phase so Ctrl+C sets
        # cancel_event (stops any in-flight backup/restore subprocess) before
        # raising KeyboardInterrupt.  Python's with-block __exit__ handles
        # IOSBackupInjector and SessionManager cleanup automatically.
        _orig_sigint = signal.getsignal(signal.SIGINT)
        _cancel_ev_ref = getattr(self.config, 'cancel_event', None)

        def _sigint_handler(sig: int, frame: object) -> None:
            logger.warning(
                "pipeline: Ctrl+C — aborting transfer; partial iOS staging "
                "may remain at %s for manual recovery",
                self.config.temp_dir / "ios_repacked",
            )
            if _cancel_ev_ref is not None:
                _cancel_ev_ref.set()
            signal.signal(signal.SIGINT, _orig_sigint)
            raise KeyboardInterrupt

        signal.signal(signal.SIGINT, _sigint_handler)
        _lock_path = (self._config or get_config()).temp_dir / f"pipeline_{src_id}_{dst_id}.lock"
        with _pipeline_lock(_lock_path, src_id, dst_id), _dest_injector_ctx as _dest_injector, session_manager as sm:
            # ── Phase A: build extract/inject closures for every category ──
            _extract_fns: dict[str, Callable[[], list]] = {}
            _inject_fns: dict[str, Callable[[list], int]] = {}

            for category, extractor, injector in runnable:
                cat_staging = sm.staging_path(category)
                cat_staging.mkdir(parents=True, exist_ok=True)

                if wifi_src is not None:
                    def make_extract_fn(
                        wx=wifi_src, cat=category, staging=cat_staging,
                    ) -> Callable[[], list]:
                        def _extract() -> list:
                            return wx.extract(cat, staging)
                        return _extract
                else:
                    def make_extract_fn(
                        ext: Callable,
                        staging=cat_staging,
                        dev_id=src_id,
                        privileged=src_privileged,
                    ) -> Callable[[], list]:
                        def _extract() -> list:
                            return ext(dev_id, staging, privileged)
                        return _extract

                if wifi_dst is not None:
                    def make_inject_fn(
                        wx=wifi_dst, cat=category, staging=cat_staging,
                    ) -> Callable[[list], int]:
                        def _inject(items: list) -> int:
                            return wx.inject(cat, items, staging)
                        return _inject
                else:
                    def make_inject_fn(
                        inj: Callable,
                        staging=cat_staging,
                        dev_id=dst_id,
                        privileged=dst_privileged,
                    ) -> Callable[[list], int]:
                        def _inject(items: list) -> int:
                            return inj(dev_id, items, staging, privileged)
                        return _inject

                if wifi_src is not None:
                    extract_fn = make_extract_fn()
                else:
                    extract_fn = make_extract_fn(extractor)

                if wifi_dst is not None:
                    inject_fn = make_inject_fn()
                else:
                    inject_fn = make_inject_fn(injector)

                # Wrap extract_fn to capture returned items for the archive.
                def _make_capture(fn: Callable, cat: str = category) -> Callable[[], list]:
                    def _capturing_extract() -> list:
                        items = fn()
                        _archive_items[cat] = items
                        return items
                    return _capturing_extract

                _extract_fns[category] = _make_capture(extract_fn)
                _inject_fns[category] = inject_fn

            # ── Phase B+C: pipelined extraction + injection ───────────────
            # Extractions run concurrently (backup/DB categories in a
            # multi-worker pool; USB-bound categories in a single-worker pool
            # to avoid device contention).  Each extractor pushes its result
            # onto a shared queue; the main thread consumes from that queue
            # and injects immediately, so injection starts as soon as the
            # first category's extraction finishes rather than waiting for
            # all extractions to complete.
            parallel_cats = [c for c, _, _ in runnable if c not in _USB_BOUND_CATEGORIES]
            sequential_cats = [c for c, _, _ in runnable if c in _USB_BOUND_CATEGORIES]
            n_submitted = len(runnable)

            result_queue: "_queue.Queue[tuple[str, list | Exception]]" = _queue.Queue()

            def _run_extraction(cat: str) -> None:
                """Run a single category extraction and push result to queue."""
                try:
                    staging = str(sm._staging_dir)
                    from core import session_file as _sf
                    _sf.mark_category_running(staging, cat, str(sm.staging_path(cat)))
                    sm._progress.set_total(cat, 0)
                    items = _extract_fns[cat]()
                    sm._progress.set_total(cat, len(items))
                    logger.debug("Extract complete: %s → %d items", cat, len(items))
                    result_queue.put((cat, items))
                except Exception as exc:
                    logger.error("Extraction failed for '%s': %s", cat, exc)
                    result_queue.put((cat, exc))

            if parallel_cats:
                logger.info(
                    "pipeline: parallel extraction of %d categories "
                    "(max %d workers): %s",
                    len(parallel_cats), _MAX_EXTRACT_WORKERS,
                    ", ".join(parallel_cats),
                )

            with ThreadPoolExecutor(
                max_workers=min(_MAX_EXTRACT_WORKERS, max(len(parallel_cats), 1)),
                thread_name_prefix="extract-par",
            ) as par_pool, ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="extract-seq",
            ) as seq_pool:

                for cat in parallel_cats:
                    par_pool.submit(_run_extraction, cat)

                for cat in sequential_cats:
                    if _adb is not None:
                        _notify_companion(
                            _adb, dst_id,
                            cat.replace("_", " ").title(),
                            done=0, total=1,
                        )
                    seq_pool.submit(_run_extraction, cat)

                # Consumer: inject as each extraction result arrives.
                # Runs in the main thread (single-threaded) to avoid write
                # contention on the destination device.
                for _ in range(n_submitted):
                    category, items_or_exc = result_queue.get()

                    if isinstance(items_or_exc, Exception):
                        extracted_count = len(_archive_items.get(category, []))
                        sm._progress.mark_error(category)
                        from core import session_file as _sf
                        _sf.mark_category_failed(
                            str(sm._staging_dir), category, str(items_or_exc),
                        )
                        results[category] = {
                            "status": "failed",
                            "extracted": extracted_count,
                            "injected": 0,
                            "error": str(items_or_exc),
                        }
                        if _adb is not None:
                            _notify_companion(_adb, dst_id, None)
                        continue

                    items = items_or_exc or []
                    extracted_count = len(items)

                    # Dedup: filter out items already transferred in prior sessions
                    dedup_skipped = 0
                    if _dedup is not None and items:
                        pre_count = len(items)
                        items = _dedup.filter_duplicates(category, items)
                        dedup_skipped = pre_count - len(items)

                    if _adb is not None:
                        human_label = category.replace("_", " ").title()
                        _notify_companion(_adb, dst_id, human_label, done=0, total=1)

                    # iOS dest: snapshot baseline row/entry count for this
                    # category right before injection.  Used by
                    # verify_after_commit to assert the count grew by at
                    # least the number of items injected.
                    if (
                        _dest_injector is not None
                        and items
                        and category not in _dest_baselines
                    ):
                        try:
                            from core.ios_backup_verify import take_baseline
                            base = take_baseline(_dest_injector, category)
                            if base is not None:
                                _dest_baselines[category] = base
                        except Exception as exc:
                            logger.debug(
                                "pipeline: baseline snapshot for %s failed "
                                "(non-fatal, verify will skip this category): %s",
                                category, exc,
                            )

                    try:
                        inject_fn = _inject_fns[category]
                        if self.dry_run:
                            injected_count: int = len(items)
                            logger.info(
                                "dry_run: would inject %d %s item(s) — skipped",
                                len(items), category,
                            )
                        else:
                            injected_count = inject_fn(items)
                        failed_count = max(0, len(items) - injected_count)

                        # Record successful transfers in the dedup store
                        if _dedup is not None and injected_count > 0:
                            _dedup.mark_transferred(category, items[:injected_count])

                        sm._progress.increment(category, injected_count + dedup_skipped)
                        if failed_count:
                            sm._progress.increment(category, failed_count, failed=True)
                        sm._progress.mark_done(category)

                        from core import session_file as _sf
                        _sf.mark_category_complete(
                            str(sm._staging_dir), category,
                            extracted_count,
                            injected_count + dedup_skipped,
                            failed_count,
                        )
                        logger.info(
                            "Category '%s' complete — extracted=%d  injected=%d  "
                            "dedup_skipped=%d  failed=%d",
                            category, extracted_count, injected_count,
                            dedup_skipped, failed_count,
                        )
                        results[category] = {
                            "status": "completed",
                            "extracted": extracted_count,
                            "injected": injected_count,
                            "error": None,
                        }
                    except Exception as exc:
                        logger.error(
                            "Category '%s' inject failed: %s; continuing pipeline.",
                            category, exc,
                        )
                        sm._progress.mark_error(category)
                        from core import session_file as _sf
                        _sf.mark_category_failed(
                            str(sm._staging_dir), category, str(exc),
                        )
                        results[category] = {
                            "status": "failed",
                            "extracted": extracted_count,
                            "injected": 0,
                            "error": str(exc),
                        }
                    finally:
                        if _adb is not None:
                            _notify_companion(_adb, dst_id, None)

            # ── iOS destination: commit the repacked backup ─────────────
            if not self.dry_run and _dest_injector is not None:
                _commit_ok = False
                _output_root: Path | None = None
                try:
                    _ios_cfg = self._config or get_config()
                    # IOSBackupRepacker.commit writes to <output>/<files> —
                    # but pymobiledevice3 backup2 restore expects a parent
                    # directory containing <udid>/Manifest.plist.  Mirror
                    # that layout: pass <root>/<udid> as commit target so
                    # <root> can be handed to restore unchanged.
                    _output_root = _ios_cfg.temp_dir / "ios_repacked"
                    output_dir = _output_root / dst_id
                    stats = _dest_injector.commit(output_dir)
                    _commit_ok = True
                    logger.info(
                        "pipeline: iOS backup repacked at %s "
                        "(overrides=%d additions=%d deletions=%d, %.1fs)",
                        stats.output_dir, stats.overrides, stats.additions,
                        stats.deletions, stats.duration_seconds,
                    )
                except Exception as exc:
                    logger.error(
                        "pipeline: iOS backup commit failed: %s", exc
                    )

                # ── Post-commit safety gates ────────────────────────
                # Hard-fail if any inject_*_ios silently degraded to AFC,
                # or if the repacked backup doesn't actually contain the
                # rows we just injected.  Either condition blocks the
                # restore step — pushing a broken backup to a real
                # iPhone is the worst outcome.
                _safety_failures: list[str] = []

                if _fallback_detector is not None and _fallback_detector.fallbacks:
                    for f in _fallback_detector.fallbacks:
                        _safety_failures.append(f"backup-mod fallback: {f}")
                        logger.error("pipeline: %s", f)

                if _commit_ok and _output_root is not None:
                    try:
                        from core.ios_backup_verify import verify_after_commit
                        from core.device_connection_cache import (
                            get_backup_password as _get_pw,
                        )
                        _verify_pw = (
                            (self._config or get_config()).backup_password
                            or _get_pw(dst_id)
                        )
                        if not _verify_pw:
                            _safety_failures.append(
                                "verify: no backup password available — "
                                "cannot re-decrypt repack"
                            )
                        else:
                            _injected_counts = {
                                cat: r.get("injected", 0)
                                for cat, r in results.items()
                                if r.get("status") == "completed"
                            }
                            _vresult = verify_after_commit(
                                repacked_backup_dir=_output_root / dst_id,
                                passphrase=_verify_pw,
                                baselines=_dest_baselines,
                                injected_counts=_injected_counts,
                            )
                            for line in _vresult.checked:
                                logger.info("pipeline: verify  %s", line)
                            for cat in _vresult.skipped:
                                logger.debug(
                                    "pipeline: verify skipped %s "
                                    "(no strategy or 0 injected)", cat,
                                )
                            for f in _vresult.failures:
                                _safety_failures.append(f"verify: {f}")
                                logger.error("pipeline: verify FAIL %s", f)
                    except Exception as exc:
                        _safety_failures.append(f"verify: pass crashed: {exc}")
                        logger.error(
                            "pipeline: verify pass crashed: %s", exc
                        )

                if _safety_failures:
                    _orig_snap = getattr(self, "_dest_original_backup_dir", None)
                    logger.error(
                        "pipeline: %d safety failure(s) — blocking restore "
                        "and marking iOS dest categories as failed.  "
                        "Repacked backup left at %s for inspection.%s",
                        len(_safety_failures), _output_root,
                        f"  Original snapshot at {_orig_snap}." if _orig_snap else "",
                    )
                    for cat, r in results.items():
                        if r.get("status") == "completed":
                            r["status"] = "failed"
                            r["error"] = (
                                "iOS post-commit safety check failed: "
                                + _safety_failures[0]
                            )

                if _safety_failures:
                    pass  # restore is blocked — fall through past the if
                elif _commit_ok and _output_root is not None:
                    try:
                        from core.device_connection_cache import get_backup_password
                        from core.settings_manager import get_settings
                        if get_settings().ios_auto_restore_modified_backup:
                            mgr = getattr(self, "_dest_backup_mgr", None)
                            if mgr is None:
                                logger.warning(
                                    "pipeline: auto-restore enabled but no "
                                    "dest BackupManager — skipping restore"
                                )
                            else:
                                _ios_cfg = self._config or get_config()
                                pw = (
                                    _ios_cfg.backup_password
                                    or get_backup_password(dst_id)
                                )
                                restored = mgr.restore_backup(
                                    backup_root=_output_root,
                                    password=pw,
                                    live=True,
                                    on_progress=getattr(
                                        self, "_backup_progress_cb", None
                                    ),
                                )
                                if restored:
                                    logger.info(
                                        "pipeline: restored modified backup "
                                        "to %s", dst_id,
                                    )
                                else:
                                    logger.error(
                                        "pipeline: restore to %s failed — "
                                        "modified backup remains at %s "
                                        "for manual recovery",
                                        dst_id, _output_root,
                                    )
                        else:
                            logger.info(
                                "pipeline: ios_auto_restore_modified_backup "
                                "is OFF — modified backup left at %s for "
                                "manual restore", _output_root,
                            )
                    except Exception as exc:
                        logger.error(
                            "pipeline: post-commit restore step failed: %s",
                            exc,
                        )

        # Detach the iOS fallback log handler (best-effort).
        if _fallback_detector is not None:
            try:
                logging.getLogger().removeHandler(_fallback_detector)
            except Exception:
                pass

        # Restore the SIGINT handler now that the transfer section is complete.
        try:
            signal.signal(signal.SIGINT, _orig_sigint)
        except Exception:
            pass

        # Persist the dedup store so future sessions skip already-transferred items.
        if _dedup is not None:
            try:
                _dedup.save()
                logger.info("pipeline: dedup store saved — %s", _dedup.stats)
            except Exception as exc:
                logger.error("pipeline: dedup save failed — future sessions may re-transfer duplicates: %s", exc)

        # Close the parallel transfer pool (if opened).
        if _transfer_pool is not None:
            try:
                _transfer_pool.close()
            except Exception:
                pass

        # Close any Wi-Fi sessions opened for this pipeline run.
        for wx in (wifi_src, wifi_dst):
            if wx is not None:
                try:
                    wx._session.disconnect()
                except Exception:
                    pass

        # Pack all captured extraction data into a universal backup archive
        # (.ptbak).  Applies to both iOS and Android sources.
        # Failures here are non-fatal — transfer results are already committed.
        _archive_path_str: str | None = None
        if _archive_items:
            try:
                from core.universal_backup import BackupArchive, archive_path_for
                _arc_cfg = self._config or get_config()
                _arc_path = archive_path_for(src_id, _arc_cfg.archive_dir)
                _meta: dict = {
                    "serial": src_id,
                    "device_name": getattr(self.source, "name", src_id),
                    "os_version": getattr(self.source, "os_version", ""),
                    "source_platform": self.source.platform,
                    "categories": sorted(_archive_items.keys()),
                }
                BackupArchive(_arc_path).create(
                    session_manager.staging_dir,
                    _archive_items,
                    _meta,
                )
                _archive_path_str = str(_arc_path)
                logger.info("pipeline: universal archive → %s", _arc_path)
            except Exception as exc:
                logger.warning(
                    "pipeline: archive creation failed (non-fatal): %s", exc
                )

        # Clean up companion staging directories on the Android device.
        # The companion APK leaves extraction artifacts in known locations
        # that accumulate across runs.  We delete them via ADB after a
        # successful transfer (best-effort; failures are non-fatal).
        _android_serial = (
            dst_id if self.destination.platform == "android" else
            src_id if self.source.platform == "android" else None
        )
        if _android_serial and _adb is not None:
            _COMPANION_STAGING_DIRS = [
                "Documents/PhoneTransfer",
                "DCIM/PhoneTransfer",
                "Music/PhoneTransfer",
            ]
            for _stg_dir in _COMPANION_STAGING_DIRS:
                try:
                    _adb.shell(
                        _android_serial,
                        f"rm -rf /sdcard/{_stg_dir}",
                        timeout=15,
                    )
                    logger.debug(
                        "pipeline: cleaned up companion staging dir /sdcard/%s on %s",
                        _stg_dir, _android_serial,
                    )
                except Exception as _rm_exc:
                    logger.debug(
                        "pipeline: companion staging cleanup failed for "
                        "/sdcard/%s (non-fatal): %s", _stg_dir, _rm_exc,
                    )

        # Optionally delete the iOS backup now that all extractors and the
        # archive writer are done.
        if _backup_mgr is not None:
            try:
                from core.settings_manager import get_settings as _get_settings
                if _get_settings().ios_delete_backup_after_extract:
                    _backup_mgr.delete_backup_if_safe()
            except Exception as exc:
                logger.warning("pipeline: post-extract cleanup failed (non-fatal): %s", exc)

        summary = {
            "session_id": session_manager.session_id,
            "source": {
                "platform": self.source.platform,
                "serial": self.source.serial,
            },
            "destination": {
                "platform": self.destination.platform,
                "serial": self.destination.serial,
            },
            "categories": results,
            "archive_path": _archive_path_str,
        }

        logger.info(
            "Pipeline finished — session=%s  completed=%d  failed=%d  skipped=%d",
            summary["session_id"],
            sum(1 for v in results.values() if v["status"] == "completed"),
            sum(1 for v in results.values() if v["status"] == "failed"),
            sum(1 for v in results.values() if v["status"] == "skipped"),
        )

        # Post-transfer reboot for iOS destinations: send only after ALL
        # categories have finished so contacts, SMS, etc. are not cut short.
        if self.destination.platform == "ios":
            try:
                cfg = self._config or get_config()
                if cfg.reboot_after_ios_photos:
                    any_injected = any(
                        v.get("injected", 0) > 0 for v in results.values()
                    )
                    if any_injected:
                        from core.ios_service_broker import IOSServiceBroker
                        broker = IOSServiceBroker(udid=dst_id)
                        try:
                            logger.info(
                                "pipeline: all categories complete — sending "
                                "reboot command to iOS device %s", dst_id
                            )
                            broker.reboot_device()
                        finally:
                            broker.close()
            except Exception as exc:
                logger.warning("pipeline: post-transfer reboot failed: %s", exc)

        return summary

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_extractor(self, category: str, platform: str) -> Callable | None:
        """
        Attempt a dynamic import of ``core.extract_{category}_{platform}``
        and return its ``extract`` callable.

        Returns ``None`` if the module cannot be imported or does not expose
        an ``extract`` attribute.

        Parameters
        ----------
        category:
            Category name, e.g. ``"contacts"``.
        platform:
            Platform string, e.g. ``"ios"`` or ``"android"``.
        """
        return self._load_callable(
            module_name=f"core.extract_{category}_{platform}",
            attr="extract",
            role="extractor",
            category=category,
            platform=platform,
        )

    def _open_dest_ios_backup_injector(
        self, dst_udid: str
    ) -> contextlib.AbstractContextManager:
        """
        Return a context manager that yields an :class:`IOSBackupInjector`
        bound to the destination iPhone's backup, or a no-op
        ``nullcontext`` if no usable backup is available.

        The injector is what each ``inject_*_ios`` module reaches for via
        :func:`core.ios_backup_injector.get_current_injector`; without one
        active, those modules fall back to AFC pushes / JSON exports.
        """
        try:
            from core.backup_manager_ios import BackupManager
            from core.device_connection_cache import get_backup_password
            from core.ios_backup_injector import IOSBackupInjector
        except Exception as exc:
            logger.warning(
                "pipeline: iOS backup injector unavailable (%s) — "
                "destination injectors will use their fallback paths", exc,
            )
            return contextlib.nullcontext()

        cfg = self._config or get_config()
        try:
            mgr = BackupManager(udid=dst_udid, config=cfg)
        except Exception as exc:
            logger.warning(
                "pipeline: BackupManager init for dest %s failed: %s — "
                "destination injectors will use their fallback paths",
                dst_udid, exc,
            )
            return contextlib.nullcontext()

        if not mgr.backup_dir.exists() or not (mgr.backup_dir / "Manifest.plist").exists():
            logger.info(
                "pipeline: no destination backup at %s — running a fresh "
                "encrypted backup of the destination iPhone first",
                mgr.backup_dir,
            )
            try:
                ok = mgr.ensure_backup_for_transfer(
                    on_progress=getattr(self, "_backup_progress_cb", None),
                    on_password_needed=getattr(
                        self, "_password_needed_cb", None
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "pipeline: dest backup creation failed (%s) — "
                    "backup-mod path skipped", exc,
                )
                return contextlib.nullcontext()
            if not ok:
                logger.warning(
                    "pipeline: dest backup unavailable — backup-mod path "
                    "skipped; injectors will fall back to AFC/JSON"
                )
                return contextlib.nullcontext()

        password = (
            get_backup_password(dst_udid)
            or getattr(cfg, "backup_password", None)
        )
        if not password:
            logger.warning(
                "pipeline: destination backup found but no password "
                "registered — backup-mod path skipped"
            )
            return contextlib.nullcontext()

        staging_root = cfg.temp_dir / "ios_inject" / dst_udid
        logger.info(
            "pipeline: opening iOS backup injector for dest %s "
            "(source=%s, staging=%s)",
            dst_udid, mgr.backup_dir, staging_root,
        )
        # Stash the BackupManager so the post-commit hook can run restore
        # without re-deriving its config.
        self._dest_backup_mgr = mgr

        # Snapshot destination backup via hardlinks so a restore-gone-wrong
        # can be manually recovered without re-running the full device backup.
        _snap_dir = cfg.temp_dir / "ios_original" / dst_udid
        try:
            _snapped = _hardlink_backup(mgr.backup_dir, _snap_dir)
            self._dest_original_backup_dir: Path | None = _snap_dir
            logger.info(
                "pipeline: snapshotted destination backup (%d files) → %s",
                _snapped, _snap_dir,
            )
        except Exception as _snap_exc:
            logger.warning(
                "pipeline: destination backup snapshot failed (%s) — "
                "manual rollback will not be available", _snap_exc,
            )
            self._dest_original_backup_dir = None

        return IOSBackupInjector.open(
            udid=dst_udid,
            source_backup_dir=mgr.backup_dir,
            passphrase=password,
            staging_root=staging_root,
        )

    def _get_injector(self, category: str, platform: str) -> Callable | None:
        """
        Attempt a dynamic import of ``core.inject_{category}_{platform}``
        and return its ``inject`` callable.

        Returns ``None`` if the module cannot be imported or does not expose
        an ``inject`` attribute.

        Parameters
        ----------
        category:
            Category name, e.g. ``"contacts"``.
        platform:
            Platform string, e.g. ``"ios"`` or ``"android"``.
        """
        return self._load_callable(
            module_name=f"core.inject_{category}_{platform}",
            attr="inject",
            role="injector",
            category=category,
            platform=platform,
        )

    @staticmethod
    def _load_callable(
        module_name: str,
        attr: str,
        role: str,
        category: str,
        platform: str,
    ) -> Callable | None:
        """
        Import *module_name* and return ``getattr(module, attr)``, or
        ``None`` if the import fails or the attribute is absent.

        Parameters
        ----------
        module_name:
            Dotted module path, e.g. ``"core.extract_contacts_ios"``.
        attr:
            Name of the callable to retrieve from the module.
        role:
            Human-readable label used in log messages (``"extractor"`` or
            ``"injector"``).
        category:
            Category name for logging context.
        platform:
            Platform name for logging context.
        """
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            logger.debug(
                "No %s found for category='%s' platform='%s' (module '%s' not found).",
                role,
                category,
                platform,
                module_name,
            )
            return None
        except ImportError as exc:
            logger.warning(
                "Import error loading %s for category='%s' platform='%s': %s",
                role,
                category,
                platform,
                exc,
            )
            return None

        fn = getattr(module, attr, None)
        if fn is None:
            logger.warning(
                "Module '%s' exists but has no '%s' attribute; skipping.",
                module_name,
                attr,
            )
            return None

        if not callable(fn):
            logger.warning(
                "Module '%s' attribute '%s' is not callable (got %r); skipping.",
                module_name,
                attr,
                type(fn),
            )
            return None

        logger.debug(
            "Loaded %s for category='%s' platform='%s' from '%s'.",
            role,
            category,
            platform,
            module_name,
        )
        return fn

    def __repr__(self) -> str:
        return (
            f"PipelineManager("
            f"src={self.source.platform}:{self.source.serial}, "
            f"dst={self.destination.platform}:{self.destination.serial}, "
            f"categories={self.categories})"
        )
