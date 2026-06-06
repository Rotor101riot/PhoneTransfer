"""
device_quirks.py

Runtime detection of OEM-specific device quirks that affect transfer
behaviour.  Queries the companion APK's ``device_info`` response to
determine which workarounds to apply.

Detected quirks
---------------
- **EMUI** (Huawei/Honor): ``ro.build.version.emui`` property.
  EMUI devices require:
  - EmotionMedia ContentProvider for playlists
  - Custom voice memo directories (/sdcard/Sounds, /sdcard/record)
  - HuaweiBackup directory scanning
  - EMUI-specific storage path resolution

- **MIUI** (Xiaomi/Redmi/POCO): ``ro.miui.ui.version.name`` property.
  MIUI devices require:
  - Non-standard SMS role change flow (Item #14)
  - MIUI Optimization must be disabled for ADB
  - SIM card may be required for USB debugging
  - Custom sound recorder directory (/sdcard/MIUI/sound_recorder)

- **HyperOS** (Xiaomi next-gen): ``ro.mi.os.version.name`` property.
  Successor to MIUI, retains most MIUI-specific quirks.

- **OneUI** (Samsung): ``ro.build.version.oneui`` property.
  Samsung devices may have secure folder data isolation.

Usage
-----
    from core.device_quirks import DeviceQuirks

    quirks = DeviceQuirks.from_device_info(client.device_info())
    if quirks.is_emui:
        # enable EmotionMedia playlist extraction
    if quirks.is_miui:
        # use Xiaomi SMS role workaround
    if quirks.needs_sms_workaround:
        client.acquire_sms_role_xiaomi()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceQuirks:
    """
    Immutable snapshot of device quirk flags derived from ``device_info``.
    """

    # ── Identity ──
    manufacturer: str = ""
    model: str = ""
    brand: str = ""
    sdk_int: int = 0

    # ── OEM skin versions (None = not present) ──
    emui_version: str | None = None
    miui_version: str | None = None
    hyperos_version: str | None = None
    oneui_version: str | None = None

    # ── Derived flags ──

    @property
    def is_emui(self) -> bool:
        """True if this is a Huawei/Honor device running EMUI."""
        return self.emui_version is not None

    @property
    def is_miui(self) -> bool:
        """True if this is a Xiaomi/Redmi/POCO device running MIUI."""
        return self.miui_version is not None

    @property
    def is_hyperos(self) -> bool:
        """True if running Xiaomi HyperOS (MIUI successor)."""
        return self.hyperos_version is not None

    @property
    def is_xiaomi_family(self) -> bool:
        """True for any Xiaomi/Redmi/POCO device (MIUI or HyperOS)."""
        if self.is_miui or self.is_hyperos:
            return True
        return any(
            b in self.manufacturer.lower()
            for b in ("xiaomi", "redmi", "poco")
        )

    @property
    def is_huawei_family(self) -> bool:
        """True for Huawei or Honor devices regardless of EMUI presence."""
        return any(
            b in self.manufacturer.lower()
            for b in ("huawei", "honor")
        )

    @property
    def is_samsung(self) -> bool:
        """True for Samsung devices."""
        return "samsung" in self.manufacturer.lower()

    @property
    def is_oneui(self) -> bool:
        """True if running Samsung OneUI."""
        return self.oneui_version is not None

    # ── Transfer behaviour flags ──

    @property
    def needs_sms_workaround(self) -> bool:
        """True if SMS role acquisition needs the Xiaomi MIUI workaround."""
        return self.is_xiaomi_family

    @property
    def has_emotion_media(self) -> bool:
        """True if the device likely has Huawei's EmotionMedia ContentProvider."""
        return self.is_emui or self.is_huawei_family

    @property
    def has_custom_voice_memo_dirs(self) -> bool:
        """True if the device has OEM-specific voice memo directories."""
        return self.is_emui or self.is_miui or self.is_xiaomi_family

    @property
    def extra_voice_memo_dirs(self) -> list[str]:
        """OEM-specific voice memo directories to scan in addition to standard ones."""
        dirs: list[str] = []
        if self.is_emui or self.is_huawei_family:
            dirs.extend([
                "/sdcard/Sounds",
                "/sdcard/record",
                "/sdcard/Recorder",
                "/sdcard/HuaweiBackup/backupFiles",
            ])
        if self.is_miui or self.is_xiaomi_family:
            dirs.append("/sdcard/MIUI/sound_recorder")
        return dirs

    @property
    def needs_miui_optimization_warning(self) -> bool:
        """True if the user should be warned about MIUI Optimization."""
        return self.is_xiaomi_family

    @property
    def needs_sim_for_usb_debug(self) -> bool:
        """True if a SIM card may be required for USB debugging."""
        return self.is_xiaomi_family

    @property
    def adb_extra_wait(self) -> float:
        """Extra seconds to wait after ADB operations on quirky devices."""
        if self.is_xiaomi_family:
            return 2.0  # MIUI's ADB handler is slow to acknowledge
        if self.is_emui:
            return 1.5  # EMUI occasionally delays ADB responses
        return 0.0

    # ── Constructor ──

    @classmethod
    def from_device_info(cls, info: dict[str, Any]) -> DeviceQuirks:
        """
        Build a DeviceQuirks instance from a ``device_info`` response dict.

        Parameters
        ----------
        info:
            The dict returned by :meth:`CompanionClient.device_info`.
        """
        manufacturer = str(info.get("manufacturer", ""))
        model = str(info.get("model", ""))
        brand = str(info.get("brand", ""))
        sdk_int = int(info.get("sdk_int", 0))

        emui = info.get("emui_version")
        miui = info.get("miui_version")

        # HyperOS and OneUI aren't in the current device_info response
        # but future APK versions may add them.  Check both forms.
        hyperos = info.get("hyperos_version") or info.get("hyper_os_version")
        oneui = info.get("oneui_version") or info.get("one_ui_version")

        quirks = cls(
            manufacturer=manufacturer,
            model=model,
            brand=brand,
            sdk_int=sdk_int,
            emui_version=emui if emui else None,
            miui_version=miui if miui else None,
            hyperos_version=str(hyperos) if hyperos else None,
            oneui_version=str(oneui) if oneui else None,
        )

        logger.info(
            "Device quirks: %s %s (SDK %d) — EMUI=%s MIUI=%s HyperOS=%s OneUI=%s",
            manufacturer, model, sdk_int,
            emui or "no", miui or "no",
            hyperos or "no", oneui or "no",
        )

        return quirks

    @classmethod
    def unknown(cls) -> DeviceQuirks:
        """Return a safe-default instance when device info is unavailable."""
        return cls()
