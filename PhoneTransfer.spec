# -*- mode: python ; coding: utf-8 -*-
# PhoneTransfer.spec — PyInstaller build specification
#
# Build command (from the project root):
#   pyinstaller PhoneTransfer.spec
#
# Output: dist/PhoneTransfer/PhoneTransfer.exe  (one-folder build)
#
# Notes:
#   - bin/ subdirectories (adb, ffmpeg, libimobiledevice) are bundled as-is
#     via the datas list so they remain as real files on disk at runtime.
#   - The app must resolve config_loader.get_config() at startup, which walks
#     upward from __file__ looking for core/ and bin/ siblings.  Because
#     PyInstaller places everything under _MEIPASS, the walk will find the
#     correct root automatically.
#   - hiddenimports covers packages that PyInstaller misses via static analysis
#     (dynamic importlib.import_module calls in pipeline_manager.py, and all
#     extractor/injector modules transitively imported from hidden modules).
#   - DO NOT add tkinter to excludes — customtkinter depends on tkinter.

import os
from pathlib import Path

HERE = Path(SPEC).resolve().parent  # project root at build time

# ── Collect bundled binaries ───────────────────────────────────────────────────

_bin_datas = []
for subdir in ("adb", "ffmpeg", "libimobiledevice", "drivers", "redist"):
    src = HERE / "bin" / subdir
    if src.is_dir():
        _bin_datas.append((str(src), f"bin/{subdir}"))

# Reference JSON files and device lookup tables
_ref_datas = [(str(HERE / "reference"), "reference")]

# Assets (icon, companion APK, launcher images)
_asset_datas = []
assets_dir = HERE / "assets"
if assets_dir.is_dir():
    _asset_datas.append((str(assets_dir), "assets"))

# customtkinter theme assets (required at runtime — CTk reads them from disk)
import customtkinter
_ctk_path = Path(customtkinter.__file__).parent
_ctk_datas = [
    (str(_ctk_path / "assets"), "customtkinter/assets"),
]

# Package data files and submodule discovery
from PyInstaller.utils.hooks import collect_data_files as _cdf, collect_submodules as _csm

# iphone_backup_decrypt ships key-derivation tables as package data
_ibd_datas = _cdf("iphone_backup_decrypt")

# pymobiledevice3 resources/ dir contains dsc_uuid_map.json, webinspector JS
# files, and notifications.txt — all read from disk at runtime.
_pmd3_datas = _cdf("pymobiledevice3")

all_datas = _bin_datas + _ref_datas + _asset_datas + _ctk_datas + _ibd_datas + _pmd3_datas

# ── Hidden imports ────────────────────────────────────────────────────────────
# All core.extract_* and core.inject_* modules are loaded via
# importlib.import_module in pipeline_manager.py — PyInstaller cannot trace
# these with static analysis.  Every extractor/injector must be listed here.

_PLATFORMS = ("ios", "android")
_CATEGORIES = (
    "alarms", "apps", "blocked", "bookmarks", "browser", "browser_history",
    "calendar", "calls", "clipboard", "contact_groups", "contacts",
    "health", "installed_apps", "mail_accounts", "notes", "photos",
    "reminders", "ringtones", "signal", "sms", "videos", "voice_memos",
    "voicemail", "wallpaper", "whatsapp",
)

_dynamic_imports = []
for _cat in _CATEGORIES:
    for _plat in _PLATFORMS:
        for _direction in ("extract", "inject"):
            _dynamic_imports.append(f"core.{_direction}_{_cat}_{_plat}")

hidden_imports = _dynamic_imports + _csm("pymobiledevice3") + [
    # convert/ modules are imported from inside hidden extractor/injector
    # modules — PyInstaller cannot trace through them automatically.
    "convert.convert_alarms",
    "convert.convert_audio",
    "convert.convert_blocked",
    "convert.convert_bookmarks",
    "convert.convert_calendar",
    "convert.convert_calllog",
    "convert.convert_contacts",
    "convert.convert_health",
    "convert.convert_heic",
    "convert.convert_imessage_to_mms",
    "convert.convert_mms_attachments",
    "convert.convert_notes",
    "convert.convert_ringtones",
    "convert.convert_signal",
    "convert.convert_sms",
    "convert.convert_video",
    "convert.convert_whatsapp",
    # validate/ modules loaded via importlib in validate/__init__.py
    "validate.validate_contacts",
    "validate.validate_sms",
    "validate.validate_calllog",
    "validate.validate_calendar",
    "validate.validate_notes",
    "validate.validate_media",
    "validate.validate_signal",
    "validate.validate_whatsapp",
    "validate.validate_health",
    # iphone_backup_decrypt and its internal modules
    "iphone_backup_decrypt",
    "iOSbackup",
    # Crypto / key derivation
    "Crypto",
    "Crypto.Cipher",
    "Crypto.Cipher.AES",
    "Crypto.Cipher.ChaCha20",
    "Crypto.Hash",
    "Crypto.Protocol.KDF",
    "cryptography",
    "cryptography.hazmat.primitives.ciphers",
    "cryptography.hazmat.primitives.kdf.pbkdf2",
    # vCard parsing
    "vobject",
    # WhatsApp backup decryption
    "wacrypt",
    # Misc runtime imports that static analysis misses
    "plistlib",
    "sqlite3",
    "xml.etree.ElementTree",
    "email.mime.text",
    "email.mime.multipart",
    "email.mime.base",
]

# ── Analysis ──────────────────────────────────────────────────────────────────

a = Analysis(
    ["main.py"],
    pathex=[str(HERE)],
    binaries=[],
    datas=all_datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # NOTE: do NOT exclude tkinter — customtkinter is built on top of tkinter.
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PhoneTransfer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX disabled: aggressive compression causes false-positive AV detection
    # on an app that accesses SMS, contacts, and call logs.
    upx=False,
    console=False,                 # no console window in production
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(HERE / "assets" / "icon.ico") if (HERE / "assets" / "icon.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PhoneTransfer",
)
