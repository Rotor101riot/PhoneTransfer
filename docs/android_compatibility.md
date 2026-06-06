# Android Version & OEM Compatibility

This document covers which Android versions and OEM firmware variants are
tested, known ContentProvider divergences, and workarounds for common
Android-specific transfer failures.

---

## Build targets

| Setting       | Value | Notes                                              |
|---------------|-------|----------------------------------------------------|
| `minSdk`      | 26    | Android 8.0 Oreo — required for JobScheduler + foreground service APIs |
| `targetSdk`   | 35    | Android 15 — edge-to-edge display, predictive back gesture |
| Companion ABI | arm64-v8a + armeabi-v7a | Fat APK; x86/x86_64 not included |

---

## Tested configurations

| Android version | OEM firmware  | Source | Dest | Notes                                        |
|-----------------|---------------|--------|------|----------------------------------------------|
| 8.0 – 9         | Stock AOSP    | ✓      | ✓    | No call-log or SMS role restrictions         |
| 10              | Pixel (stock) | ✓      | ✓    | First version requiring default-SMS-app role for SMS inject |
| 10              | Samsung One UI 2.x | ✓  | ✓    | Role grant flow differs — see Samsung section |
| 11              | Pixel (stock) | ✓      | ✓    | Call log ContentProvider restricted to default dialer |
| 12              | Pixel (stock) | ✓      | ✓    | No new restrictions vs Android 11 for this use case |
| 12              | MIUI 13 (Xiaomi) | ✓   | Partial | Battery saver kills companion mid-transfer — whitelist required |
| 13              | Pixel (stock) | ✓      | ✓    | Native HEIC support added (no JPEG conversion needed as dest) |
| 13              | One UI 5.x    | ✓      | ✓    | Samsung contacts account type quirks — see Samsung section |
| 14              | Pixel (stock) | ✓      | ✓    | Health Connect introduced (not yet integrated) |
| 14              | MIUI 14       | ✓      | Partial | Same battery-saver issue as MIUI 13 |
| 15              | Pixel (stock) | ✓      | ✓    | Edge-to-edge predictive back — companion UI updated |

---

## Android 10+ permission gates

Android 10 introduced two hard permission restrictions that affect injection:

### SMS / MMS injection (Android 10+)
The `content://sms` ContentProvider rejects writes from any app that is not
currently set as the **default SMS app**.

**Fix (companion path):** The companion APK requests the `ROLE_SMS` role via
`RoleManager`.  An on-device dialog asks the user to confirm.  After transfer,
restore your original SMS app under Settings → Apps → Default apps → SMS App.

**Fix (rooted path):** Direct SQLite write to `mmssms.db` bypasses the
ContentProvider restriction.  Enable Rooted mode in settings.

### Call log injection (Android 10+)
`content://call_log/calls` rejects inserts from any app that is not the
**default dialer**.

**Fix:** Enable Rooted mode — the rooted SQLite fallback in
`inject_calls_android.py` writes directly to `calllog.db` via `su -c sqlite3`.
There is no non-root workaround on Android 10+.

---

## OEM-specific notes

### Samsung One UI

| Issue | Detail |
|-------|--------|
| **PTP mode required** | Samsung defaults to "Charging only" USB mode. Switch to PTP or MTP before connecting. Settings → Connected devices → USB → File transfer. |
| **Knox container isolation** | Contacts, SMS, and call logs stored in the Knox Work Profile are in a separate database partition and are NOT accessible via ADB. Transfer only covers the personal profile. |
| **Contact account type** | Samsung contacts are often stored under `com.samsung.android.sync` account type rather than null (local). The injector inserts as local contacts (`account_type=''`) — Samsung devices may display them under "Phone" rather than "Samsung Account". |
| **One UI 6+ blocked numbers** | BlockedNumberContract writes may require the default dialer role on One UI 6. If blocked numbers don't transfer, enable Rooted mode. |

### Xiaomi / MIUI / Redmi / POCO

| Issue | Detail |
|-------|--------|
| **Aggressive battery saver** | MIUI battery optimization kills background services within minutes. Go to Settings → Apps → Manage apps → PhoneTransfer Companion → Battery saver → No restrictions. |
| **Dual SIM slot selection** | MIUI may prompt for a SIM slot when SMS is injected; the companion APK auto-selects SIM 1. To override, set a preferred SIM in MIUI settings before transferring. |
| **MIUI security popup** | MIUI shows a "PhoneTransfer is trying to read your data" alert for each ContentProvider access. Tap "Allow" and check "Remember". |

### Huawei / EMUI / HarmonyOS

| Issue | Detail |
|-------|--------|
| **HDB (Huawei Device Bridge)** | On HarmonyOS 3+, standard ADB may not enumerate the device. Install Huawei Device Bridge drivers from Huawei's developer site. |
| **USB mode** | Huawei defaults to "Charge only". Enable "Transfer files (MTP)" or "Transfer photos (PTP)" in the USB connection menu. |
| **No Google Play** | Huawei devices shipped without Google Play (2019+) cannot install the companion APK via Play Store. Sideload via ADB (`adb install companion.apk`). |

### OnePlus / OxygenOS

| Issue | Detail |
|-------|--------|
| **USB 3 cable issues** | USB 3.x cables sometimes cause ADB enumeration failures on OnePlus devices. Try a USB 2.0 cable first. |
| **USB mode persistence** | OxygenOS resets USB mode to "Charge" on each reconnect. Re-select "File transfer" after every cable reconnect. |
| **Aggressive RAM management** | OxygenOS can kill the companion foreground service on OnePlus 9 and earlier. Lock the companion app in Recent Apps before starting a transfer. |

### Motorola

| Issue | Detail |
|-------|--------|
| **MTP-only mode** | Some Motorola devices enumerate as MTP-only and refuse PTP. ADB works regardless of MTP/PTP selection. |
| **Stock-AOSP close** | Motorola's Android build is close to stock. ContentProvider behaviour matches Pixel in testing. |

### Sony Xperia

| Issue | Detail |
|-------|--------|
| **MTP disconnect on ADB** | Certain Xperia models disconnect MTP when ADB is active. Use ADB-only (no file manager open) during transfer. |

---

## ContentProvider divergence matrix

The following table shows which operations are confirmed to work via the
standard ContentProvider path vs which require the rooted SQLite fallback.

| Operation                  | Android 8–9 | Android 10–11 | Android 12–15 | Rooted fallback |
|----------------------------|-------------|---------------|---------------|-----------------|
| Contacts insert            | ✓ CP        | ✓ CP          | ✓ CP          | N/A             |
| SMS inject (plain text)    | ✓ CP        | Needs default SMS role | Needs default SMS role | ✓ sqlite3 |
| MMS inject                 | ✓ CP        | Needs default SMS role | Needs default SMS role | ✓ sqlite3 |
| Call log inject            | ✓ CP        | Needs default dialer   | Needs default dialer   | ✓ sqlite3 |
| Calendar insert            | ✓ CP        | ✓ CP          | ✓ CP          | ✓ sqlite3 |
| Blocked numbers insert     | ✓ CP        | ✓ CP          | ✓ CP (may need dialer on OEM) | ✓ sqlite3 |
| Photos/video push          | ✓ ADB push  | ✓ ADB push    | ✓ ADB push    | N/A             |

---

## ADB shell user limitations

All non-root ADB operations run as the `shell` user (UID 2000).  The shell
user has read/write access to external storage (`/sdcard/`) and can use
ContentProvider insert/query for most providers, but:

- Cannot read `/data/data/<package>/` without root.
- Cannot run `sqlite3` on protected databases without `su`.
- ContentProvider write access for SMS, call log, and (on some OEMs) contacts
  is gated behind app roles on Android 10+.

Enable **Rooted mode** in settings when the source or destination device is
rooted to unlock the SQLite fallback paths.

---

## Companion APK port

The companion APK listens on TCP port **7337** (forwarded via ADB).  If another
app is already on port 7337 (rare), restart the device before transferring.
PhoneTransfer verifies port ownership via `/proc/net/tcp6` UID matching before
establishing the forward — a mismatch is logged as an error and the connection
is refused.

---

## How to file an OEM-specific bug

1. Run the transfer.
2. Attach `tmp/logs/phonetransfer.log` (PII redacted automatically).
3. Include: device model, OEM firmware version (Settings → About phone → Software information), Android version, and which categories failed.
4. File at: https://github.com/<your-repo>/PhoneTransfer/issues
