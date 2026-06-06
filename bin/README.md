# bin/ — Bundled binaries

The prerequisite checker (`core/prerequisite_checker.py`) downloads and places
all required binaries automatically on first run. This document lists the
sources for manual download or auditing.

---

## FFmpeg (`bin/ffmpeg/`)

Used for audio/video conversion (AMR, HEIC, M4A, ringtone transcoding).

**Download:** https://github.com/BtbN/FFmpeg-Builds/releases/latest

Choose `ffmpeg-master-latest-win64-gpl.zip`. Extract and place:
- `ffmpeg.exe` → `bin/ffmpeg/bin/ffmpeg.exe`
- `ffplay.exe` → `bin/ffmpeg/bin/ffplay.exe`
- `ffprobe.exe` → `bin/ffmpeg/bin/ffprobe.exe`

The `bin/ffmpeg/doc/` and `bin/ffmpeg/presets/` directories are committed and
do not need to be re-downloaded.

---

## iTunes (`bin/redist/iTunes64Setup.exe`)

Required for Apple USB driver installation on Windows (needed for libimobiledevice
to communicate with iPhones over USB). Not needed if iTunes is already installed.

**Download:** https://www.apple.com/itunes/download/win64

Place the installer at `bin/redist/iTunes64Setup.exe`. The prerequisite checker
runs it silently and extracts only the Apple Mobile Device USB driver.

---

## ADB (`bin/adb/`)

Android Debug Bridge — required for Android device communication.

**Included in repo.** Source: https://developer.android.com/tools/releases/platform-tools

---

## libimobiledevice (`bin/libimobiledevice/`)

iOS device communication library (idevicebackup2, idevicepair, AFC).

**Included in repo.** Source: https://github.com/libimobiledevice/libimobiledevice

---

## Visual C++ Redistributables (`bin/redist/VC_redist.*.exe`)

Required by libimobiledevice and other native components on Windows.

**Included in repo.** Source: https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist

---

## Android USB drivers (`bin/drivers/android/`)

OEM USB drivers for various Android manufacturers, used by ADB.

**Included in repo.** Sourced from manufacturer distribution packages.

---

## Apple USB driver (`bin/drivers/apple_usb/`)

Apple Mobile Device USB driver for Windows, extracted from iTunes.

**Included in repo** (driver files only — the iTunes installer itself is not committed).
