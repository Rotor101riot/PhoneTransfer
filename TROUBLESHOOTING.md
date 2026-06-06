# Troubleshooting

Quick-reference for the most common PhoneTransfer failure modes.

---

## iOS issues

### "Trust This Computer?" prompt was missed

**Symptom:** Transfer fails immediately with `Unable to connect to device` or `usbmuxd error`.

**Cause:** The iOS device needs to trust the host machine before any backup or data operation can proceed.

**Fix:**
1. Unlock the iPhone and look for the "Trust This Computer?" alert.
2. Tap **Trust** and enter your passcode if prompted.
3. Click **Refresh Devices** in PhoneTransfer and try again.
4. If the alert never appears: unplug, re-plug the cable, then unlock the phone before plugging in.

---

### "Backup encryption password is wrong" / backup decrypt fails

**Symptom:** `iphone_backup_decrypt: wrong password` or `Error: backup decryption failed` in the log.

**Cause:** iTunes/Finder backup encryption is enabled on the device and the password entered does not match.

**Fix:**
1. Open iTunes (Windows) or Finder (Mac) → device → Summary.
2. Under Backups, check **Encrypt local backup**. The password was set when encryption was first enabled.
3. If the password is genuinely lost: on the device go to Settings → General → Transfer or Reset iPhone → Reset → Reset All Settings. This removes the backup password (and resets all settings, but does not delete data).

---

### Backup runs but categories are empty

**Symptom:** Transfer completes with 0 items for contacts, SMS, etc.

**Common causes:**
- Backup is encrypted but the PhoneTransfer decrypt step was skipped (no password provided).
- Source iOS version is significantly newer than destination (schema drift — check log for `pre-flight` warnings).
- The app data simply isn't in the backup: health data, WhatsApp, and Signal require either iCloud backup or jailbreak access.

**Fix:** Supply the backup password in the device-connection dialog; check the `phonetransfer.log` file in `tmp/logs/` for extractor-specific errors.

---

### iOS 18 source — contacts or health data missing

**Symptom:** Transfer from an iOS 18+ device produces empty contacts or health categories.

**Cause:** iOS 18 tightened backup encryption schemas in some domains. The extractors include `PRAGMA table_info` tolerance, but some column layouts changed.

**Fix:** File a bug with the `phonetransfer.log` attached (redact personal data). As a workaround, export contacts as a `.vcf` from the Contacts app and import on the destination manually.

---

### Auto-restore hangs or partially fails

**Symptom:** The `backup2 restore` subprocess runs but the device reboots into an incomplete state.

**Cause:** The backup-mod pipeline is experimental. iOS verify-after-commit passed, but restore-time keybag unwrap can still fail for injected files assigned the wrong protection class.

**Fix:**
1. The original destination backup is preserved at `tmp/ios_original/<udid>/`. Use iMazing or `pymobiledevice3 backup2 restore --system --reboot <udid> tmp/ios_original/<udid>/` to roll back.
2. Disable `ios_auto_restore_modified_backup` in settings and restore the repacked backup manually to inspect it first.

---

## Android issues

### ADB shows device as `unauthorized`

**Symptom:** ADB commands fail with `error: device unauthorized`.

**Cause:** The Android device has not granted USB debugging permission to this computer.

**Fix:**
1. On the Android device unlock the screen.
2. In the USB debugging prompt tap **Always allow from this computer**, then **Allow**.
3. If no prompt appears: Settings → Developer Options → Revoke USB debugging authorisations → re-plug the cable.
4. Run `adb devices` in a terminal to confirm the device shows as `device` (not `unauthorized`).

---

### Companion APK not responding / ping timeout

**Symptom:** `companion not responding` or `Cannot connect to companion APK at 127.0.0.1:7337` in the log.

**Causes and fixes:**
- **App not open:** Open the PhoneTransfer companion app on the Android device manually and leave it in the foreground.
- **Battery optimisation killed it:** Go to Settings → Apps → PhoneTransfer → Battery → Unrestricted (wording varies by OEM).
- **ADB forward not set:** PhoneTransfer sets this automatically, but it can fail if ADB is unauthorised (see above).
- **Port already taken:** Another process is on port 7337. Check the log for `verify_companion_identity` errors. Restart the device.

---

### SMS injection fails on Android 10+

**Symptom:** `inject_sms_android: PERMISSION_DENIED` or 0 messages injected despite rooted or companion path.

**Cause:** Android 10+ restricts ContentResolver writes to the default SMS app only.

**Fix:**
1. The companion APK requests the default SMS role. Watch for an on-device confirmation dialog — approve it.
2. If the dialog never appears: in Settings → Apps → Default apps → SMS App, set **PhoneTransfer Companion** as the default, run the transfer, then restore your preferred SMS app.

---

### Call log empty after transfer (Android 10+)

**Symptom:** The call log injector reports success but the Phone app shows no history from the source device.

**Cause:** Android 10+ restricts call log writes to the default dialer app. Content-provider inserts from ADB shell are rejected silently.

**Fix:** Enable **Rooted mode** for the source device if it is rooted — the SQLite fallback path bypasses the content-provider restriction.

---

### OEM battery saver kills the companion mid-transfer

**Symptom:** Transfer stalls partway through; `connection reset` or `heartbeat failed` in the log.

**Affected OEMs:** Xiaomi/MIUI, OnePlus OxygenOS, Huawei EMUI, Samsung One UI (aggressive mode).

**Fix:**
1. Add the PhoneTransfer companion to the battery-optimisation whitelist (Settings → Battery → Battery optimisation → All apps → PhoneTransfer → Don't optimise).
2. Keep the Android device screen on for the duration of the transfer.
3. On Xiaomi: Settings → Apps → Manage apps → PhoneTransfer → Battery saver → No restrictions.

---

### Default SMS app role not restored after transfer

**Symptom:** After transfer, your original SMS app (e.g. Google Messages, Samsung Messages) no longer receives or sends texts. The PhoneTransfer companion app appears as the default SMS app.

**Cause:** On Android 10+, SMS injection requires the companion to hold the `ROLE_SMS` default SMS role. The companion requests this role via a system dialog before injecting messages. The transfer completes, but the role is not automatically revoked when the companion finishes.

**Why it is not automatic:** Android does not allow apps to silently release the default SMS role. Restoring the previous default requires the user to confirm the change via a system dialog.

**Fix:**
1. Open Settings on the Android device.
2. Go to Apps → Default apps → SMS app (exact path varies by OEM — see below).
3. Select your preferred SMS app (e.g. Google Messages).
4. Tap **OK** in the system confirmation dialog.

| OEM | Path |
|-----|------|
| Stock Android / Pixel | Settings → Apps → Default apps → SMS app |
| Samsung One UI | Settings → Apps → Default apps → Messaging app |
| Xiaomi MIUI | Settings → Apps → Manage apps → (search "PhoneTransfer") → Set as default → turn off SMS |
| OnePlus OxygenOS | Settings → Apps → Default apps → SMS app |

**Xiaomi note:** MIUI intercepts the standard role-grant API. If the PhoneTransfer companion still shows as the default SMS app after following the steps above, go to Settings → Apps → Manage apps → your preferred SMS app → Set as default → SMS default app.

**For developers:** The role-grant flow lives in `ChangeDefaultSmsActivity.kt` in the companion APK. `HeadlessSmsSendService.kt` is a no-op stub service that only exists to satisfy Android's requirement that a valid SMS application declare a service for `ACTION_RESPOND_VIA_MESSAGE` — it stops itself immediately and performs no real work.

---

## Log file location

The full log is written to `tmp/logs/phonetransfer.log` inside the PhoneTransfer installation directory. Phone numbers and email addresses are redacted in the log file. Attach this file when reporting bugs — it contains the exact error that caused a failure.
