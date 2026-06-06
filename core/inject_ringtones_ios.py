"""
inject_ringtones_ios.py

Inject custom ringtones onto an iOS device.

Two paths:
  * **Backup-mod (default)**: when an :class:`IOSBackupInjector` session is
    active, stage each ``.m4r`` as an addition under
    ``TonesDomain:Media/iTunes_Control/Ringtones/<NAME>.m4r``, encrypted at
    protection class 4 (``NSFileProtectionCompleteUntilFirstUserAuthentication``)
    — the class iOS uses for existing entries in this domain.  After
    restore the ringtones appear in Settings > Sounds & Haptics.
  * **AFC / AFC2 (legacy)**: push the file directly to
    ``/var/mobile/Media/iTunes_Control/Ringtones`` (AFC) or
    ``/var/mobile/Library/Ringtones`` (AFC2 jailbroken).  Kept as a
    fallback for the rare case where the caller intentionally bypassed
    the backup-mod orchestrator.

Source files in non-.m4r containers (e.g. .mp3 from Android) are
transcoded to AAC-in-MPEG-4 via ffmpeg before staging.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from core.config_loader import get_config
from core.ios_backup_injector import IOSBackupInjector, get_current_injector
from core.normalization_schema import MediaFile

logger = logging.getLogger(__name__)


_TONES_DOMAIN = "TonesDomain"
_TONES_REL_DIR = "Media/iTunes_Control/Ringtones"
_TONES_PROTECTION_CLASS = 4  # NSFileProtectionCompleteUntilFirstUserAuthentication

# Legacy AFC/AFC2 destination paths
_AFC2_RINGTONE_DIR = "/var/mobile/Library/Ringtones"
_AFC_RINGTONE_DIR = "/var/mobile/Media/iTunes_Control/Ringtones"


def inject(device_id: str, items: list, staging_dir: Path, is_privileged: bool) -> int:
    ringtones = [
        mf for mf in items
        if isinstance(mf, MediaFile) and mf.album == "ringtone"
    ]
    if not ringtones:
        logger.info("No ringtone items to inject")
        return 0

    injector = get_current_injector()
    if injector is not None:
        return _inject_via_backup(injector, ringtones, staging_dir)

    if is_privileged:
        return _inject_afc2(device_id, ringtones, staging_dir)
    return _inject_afc(device_id, ringtones, staging_dir)


# ---------------------------------------------------------------------------
# Backup-mod path
# ---------------------------------------------------------------------------

def _inject_via_backup(
    injector: IOSBackupInjector,
    items: list[MediaFile],
    staging_dir: Path,
) -> int:
    """Stage each ringtone as a TonesDomain addition."""
    staged = 0
    for mf in items:
        local = _ensure_m4r(mf, staging_dir)
        if local is None:
            continue

        try:
            data = local.read_bytes()
        except OSError as exc:
            logger.warning("Cannot read ringtone %s: %s", local, exc)
            continue
        if not data:
            continue

        rel_path = f"{_TONES_REL_DIR}/{local.name}"
        injector.stage_addition(
            _TONES_DOMAIN, rel_path, data,
            protection_class=_TONES_PROTECTION_CLASS,
        )
        logger.debug("Staged ringtone %s/%s (%d bytes)",
                     _TONES_DOMAIN, rel_path, len(data))
        staged += 1

    if staged:
        logger.info(
            "Staged %d ringtone addition(s) into the active backup; "
            "they will appear in Settings > Sounds & Haptics after restore.",
            staged,
        )
    return staged


# ---------------------------------------------------------------------------
# Jailbroken — AFC2
# ---------------------------------------------------------------------------

def _inject_afc2(udid: str, items: list[MediaFile], staging_dir: Path) -> int:
    try:
        from core.device_connection_cache import get_broker
        from core.afc2_connector import AFC2Connector

        broker = get_broker(udid)
        afc = AFC2Connector(broker)

        count = _push_items(afc, _AFC2_RINGTONE_DIR, items, staging_dir)
        if count:
            logger.info(
                "Pushed %d ringtone(s) to %s. A SpringBoard / ldrestart may be "
                "required for the device to register new ringtones.",
                count, _AFC2_RINGTONE_DIR,
            )
        return count
    except Exception:
        logger.exception("AFC2 ringtone injection failed for device %s", udid)
        return 0


# ---------------------------------------------------------------------------
# Non-jailbroken — standard AFC
# ---------------------------------------------------------------------------

def _inject_afc(udid: str, items: list[MediaFile], staging_dir: Path) -> int:
    try:
        from core.device_connection_cache import get_broker
        from core.afc_connector import AFCConnector

        broker = get_broker(udid)
        afc = AFCConnector(broker)
        count = _push_items(afc, _AFC_RINGTONE_DIR, items, staging_dir)
        if count:
            logger.info("Pushed %d ringtone(s) to %s via standard AFC.", count, _AFC_RINGTONE_DIR)
        return count
    except Exception:
        logger.exception("AFC ringtone injection failed for device %s", udid)
        return 0


# ---------------------------------------------------------------------------
# Shared push logic
# ---------------------------------------------------------------------------

def _push_items(afc, remote_dir: str, items: list[MediaFile], staging_dir: Path) -> int:
    count = 0
    afc.makedirs(remote_dir)

    for mf in items:
        local_path = _ensure_m4r(mf, staging_dir)
        if local_path is None:
            continue

        remote_path = f"{remote_dir}/{local_path.name}"
        if afc.push_file(local_path, remote_path):
            logger.debug("Pushed %s -> %s", local_path, remote_path)
            count += 1
        else:
            logger.warning("Failed to push %s to %s", local_path, remote_path)

    return count


# ---------------------------------------------------------------------------
# Format conversion helper
# ---------------------------------------------------------------------------

def _ensure_m4r(mf: MediaFile, staging_dir: Path) -> Path | None:
    src = mf.local_path
    if not src or not Path(src).exists():
        logger.warning("Source file missing for ringtone %s", mf.filename)
        return None
    src = Path(src)

    if src.suffix.lower() == ".m4r":
        return src

    cfg = get_config()
    conv_dir = staging_dir / "ringtones_converted"
    try:
        conv_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("Could not create conversion dir %s", conv_dir)
        return None

    dest = conv_dir / (src.stem + ".m4r")

    try:
        from core.ffmpeg_wrapper import convert  # type: ignore
        convert(src, dest, codec="aac", bitrate="128k")
        logger.debug("Converted %s -> %s via ffmpeg_wrapper", src, dest)
        return dest
    except Exception:
        pass

    ffmpeg_exe = getattr(cfg, "ffmpeg_exe", "ffmpeg")
    cmd = [
        str(ffmpeg_exe), "-y", "-i", str(src),
        "-c:a", "aac", "-b:a", "128k",
        str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            logger.warning("ffmpeg conversion failed for %s: %s", src, proc.stderr[-500:])
            return None
        logger.debug("Converted %s -> %s", src, dest)
        return dest
    except Exception:
        logger.exception("ffmpeg conversion raised for %s", src)
        return None
