# Transfer Guides

Step-by-step walkthroughs for each of the four transfer modes. Pick the one that matches your source and destination device.

- [Android → Android](#android--android)
- [Android → iPhone](#android--iphone)
- [iPhone → Android](#iphone--android)
- [iPhone → iPhone](#iphone--iphone)

---

## Android → Android

**What happens:** PhoneTransfer silently sideloads its companion app onto both devices via ADB, extracts your data from the source, and writes it directly to the destination's content providers (contacts, SMS, call log, etc.).

### Before you start

- USB debugging must be enabled on **both** Android devices.
  - Settings → About Phone → tap Build Number 7 times → Developer Options → USB Debugging
- Both devices must authorise this PC when connected (tap "Allow" on the USB debugging prompt).
- Both devices need to be unlocked and on the home screen during the transfer.

### Steps

1. Connect the **source** Android to the PC via USB.
2. Unlock the device. Tap **Allow** if an "Allow USB debugging?" prompt appears.
3. Connect the **destination** Android to the PC (use a USB hub if you only have one port).
4. Unlock the destination. Tap **Allow** on its USB debugging prompt.
5. Launch PhoneTransfer: `python main.py`
6. The source device appears in the **left panel**. Click it to select it.
7. The destination device appears in the **right panel**. Click it to select it.
8. Choose which categories to transfer using the checkboxes. Greyed-out categories are not supported for this device pair.
9. Optionally tick **Dry run** to preview record counts without writing anything.
10. Click **Transfer**.
11. PhoneTransfer installs the companion APK on the destination automatically. The companion opens and requests permissions — **grant all permissions** on the destination device (Contacts, Phone, SMS, Storage, etc.).
12. Watch the log panel. Each category is processed in sequence.
13. When finished, review the structured summary table in the log. A copy is saved to `~/Documents/PhoneTransfer/logs/`.

### What to expect

- The companion APK installation takes 10–30 seconds. The status bar will show "Waiting for companion permissions."
- Categories run one at a time. Photos and videos take the longest.
- The destination device screen may flicker briefly as content providers are written.

### Common issues

| Problem | Fix |
|---|---|
| Device not detected | Check USB debugging is on; try a different cable (data cable, not charge-only) |
| "Allow USB debugging?" prompt not appearing | Unplug and replug; unlock the screen first |
| Companion install fails | Ensure the device allows installs from unknown sources in Developer Options |
| SMS transfer requires setting PhoneTransfer as default SMS app | Android 10+: follow the on-screen prompt to temporarily set it as default, then revert afterward |

---

## Android → iPhone

**What happens:** PhoneTransfer extracts your data from the Android, captures a full encrypted backup of the destination iPhone, injects the data into that backup, and either restores it automatically or leaves it ready for manual restore.

> The iPhone backup is modified on your PC — your data never goes through any server.

### Before you start

- USB debugging enabled on the **source** Android.
- The **destination** iPhone must be trusted on this PC — you will be prompted to tap **Trust** on the iPhone screen when you plug it in.
- iTunes (Windows) or Finder (macOS) must be installed for backup and restore.
- Know your iPhone's **backup password** if it has one, or be prepared to set one. PhoneTransfer requires an encrypted backup to access all categories — if your backup is not encrypted, it will enable encryption and ask you to set a password.
- Free disk space equal to roughly the iPhone's storage (for the backup).

### Steps

1. Connect the **source** Android to the PC via USB. Tap **Allow** on the USB debugging prompt.
2. Connect the **destination** iPhone to the PC via USB.
3. On the iPhone: tap **Trust** and enter your passcode.
4. Launch PhoneTransfer: `python main.py`
5. Select the Android as **source** (left panel).
6. Select the iPhone as **destination** (right panel).
7. Click **Transfer**.
8. PhoneTransfer will ask for the iPhone's **backup password**. Enter it if you have one. If your backup is not yet encrypted, it will prompt you to set a password — write it down.
9. The iPhone backup begins. This takes **5–30 minutes** depending on how much is on the phone. The iOS progress bar in the UI shows backup progress.
10. Once backed up, PhoneTransfer extracts data from the Android and injects it into the backup.
11. The backup is repacked and verified (integrity check runs automatically).
12. When the verification passes:
    - **If auto-restore is off** (default): the modified backup is saved to `temp/ios_repacked/<udid>/`. Restore it manually — see below.
    - **If auto-restore is on** (Settings → Transfer): the backup is restored to the iPhone automatically. The iPhone will restart during restore.

### Manual restore

Open **Finder** (macOS) or **iTunes** (Windows):
1. Select your iPhone in the sidebar.
2. Click **Restore Backup…**
3. Choose the backup from `temp/ios_repacked/<udid>/` — it will be dated today.
4. Enter your backup password when prompted.
5. Wait for the restore to complete and the iPhone to restart.

> **Note:** Auto-restore has been pipeline-verified but not yet smoke-tested on a physical device. If you are cautious, use manual restore and verify the data before trusting the result.

### Common issues

| Problem | Fix |
|---|---|
| "Trust this Computer" prompt missed | Unplug and replug the iPhone; tap Trust when it reappears |
| Backup password unknown | Reset it: Settings → General → Transfer or Reset iPhone → Reset Encrypted Backup. You will lose Health and Keychain data in the old backup. |
| "Not enough disk space" | The backup needs space on the PC. Free up space or point the staging dir to a larger drive in Settings. |
| Restore fails in iTunes/Finder | Ensure the iPhone stays connected and unlocked during restore; try a different USB cable |

---

## iPhone → Android

**What happens:** PhoneTransfer captures a full encrypted backup of the source iPhone, decrypts it on your PC, and streams each category to the destination Android via the companion app.

### Before you start

- The **source** iPhone must be trusted on this PC.
- USB debugging enabled on the **destination** Android.
- Know your iPhone's **backup password**. If your backup is not encrypted, PhoneTransfer will enable encryption and ask you to set one — write it down.
- Free disk space for the iPhone backup (roughly equal to the phone's storage).

### Steps

1. Connect the **source** iPhone to the PC via USB.
2. On the iPhone: tap **Trust** and enter your passcode.
3. Connect the **destination** Android via USB. Tap **Allow** on the USB debugging prompt.
4. Launch PhoneTransfer: `python main.py`
5. Select the iPhone as **source** (left panel).
6. Select the Android as **destination** (right panel).
7. Click **Transfer**.
8. Enter the iPhone's **backup password** when prompted (or set one if not yet encrypted).
9. PhoneTransfer captures and decrypts the iPhone backup. This takes **5–30 minutes**.
10. PhoneTransfer installs the companion APK on the Android destination automatically.
11. On the Android: **grant all permissions** when the companion opens (Contacts, Phone, SMS, Storage, etc.).
12. Data is streamed to the Android category by category.
13. When finished, review the summary in the log panel. A copy is saved to `~/Documents/PhoneTransfer/logs/`.

### What to expect

- iOS backup capture is the longest part. The progress bar in the UI tracks it.
- Photos and videos are the largest category by volume — expect several minutes for large libraries.
- Live Photos arrive on Android as separate JPEG + MOV files. The pairing is lost but both files are preserved.
- SMS read/unread state and MMS group thread structure may not be fully preserved on the Android destination.

### Common issues

| Problem | Fix |
|---|---|
| "Trust this Computer" prompt missed | Unplug and replug the iPhone |
| Backup decryption fails | Double-check the backup password; if forgotten, reset it in iPhone Settings → General → Transfer or Reset |
| SMS not appearing on Android | Android 10+ requires PhoneTransfer to be the default SMS app during injection — follow the on-screen prompt |
| Photos not showing in Android gallery | The media scanner runs after transfer; wait 1–2 minutes or reboot the Android |

---

## iPhone → iPhone

**What happens:** PhoneTransfer backs up the source iPhone (for extraction) and the destination iPhone (as the injection target), injects the source data into the destination backup, and either restores it automatically or leaves it for manual restore.

> Both iPhones are backed up to your PC. Neither backup is uploaded anywhere.

### Before you start

- **Both** iPhones must be trusted on this PC.
- Know the backup password for **both** iPhones (or be prepared to set one on each). If either backup is unencrypted, PhoneTransfer will enable encryption and ask you to set a password.
- A USB hub is recommended to keep both phones connected simultaneously. If you only have one USB port, the tool will prompt you when to swap.
- Free disk space for **two** iPhone backups.

### Steps

1. Connect the **source** iPhone to the PC via USB.
2. On the source iPhone: tap **Trust** and enter your passcode.
3. Connect the **destination** iPhone to the PC (via hub or second port).
4. On the destination iPhone: tap **Trust** and enter its passcode.
5. Launch PhoneTransfer: `python main.py`
6. Select the **source** iPhone (left panel).
7. Select the **destination** iPhone (right panel).
8. Click **Transfer**.
9. Enter the **source iPhone's backup password** when prompted.
10. Enter the **destination iPhone's backup password** when prompted.
11. PhoneTransfer backs up the **source** iPhone. This takes **5–30 minutes**.
12. PhoneTransfer backs up the **destination** iPhone. This also takes **5–30 minutes**.
13. Data is extracted from the source backup and injected into the destination backup.
14. The destination backup is repacked and integrity-verified.
15. When verification passes:
    - **Auto-restore off** (default): modified backup saved to `temp/ios_repacked/<udid>/`. Restore manually via Finder or iTunes.
    - **Auto-restore on** (Settings → Transfer): backup is restored to the destination iPhone automatically.

### Manual restore

Open **Finder** (macOS) or **iTunes** (Windows):
1. Select the **destination** iPhone in the sidebar.
2. Click **Restore Backup…**
3. Choose the backup from `temp/ios_repacked/<udid>/` — dated today.
4. Enter the destination iPhone's backup password.
5. Wait for restore and restart.

### What to expect

- This mode takes the longest — two full backups plus injection and verification.
- The restore is **additive**: existing data on the destination is not wiped. iCloud-synced data (contacts, calendar) may re-sync from iCloud after restore and appear duplicated — disable iCloud sync for affected categories before restoring if this is a concern.
- Notes rich formatting (tables, drawings) is preserved between iOS devices.

### Common issues

| Problem | Fix |
|---|---|
| Only one iPhone detected | Check both are trusted; try different USB ports or a powered hub |
| Backup password mismatch | Each phone has its own backup password — enter them separately when prompted |
| Restore rejected by iPhone ("backup is corrupt") | The repacked backup failed verification — check the log for the specific integrity error and report it |
| iCloud data duplicated after restore | Before restoring, temporarily disable iCloud sync for Contacts, Calendar, and Notes on the destination iPhone; re-enable after restore settles |

---

## General tips

- **Use the dry-run checkbox** before your first real transfer. It shows exactly how many records would be transferred per category without writing anything to the destination.
- **Transfer logs** are saved to `~/Documents/PhoneTransfer/logs/` after every run. If something looks wrong, the log shows extracted vs. injected counts per category and any errors.
- **Large libraries** (photos, videos): expect the transfer to take 30–90 minutes for anything over 10 GB. Keep both devices plugged in and the PC awake.
- **Retry failed categories:** if one category fails mid-transfer, use the "Retry Failed" button to re-run only the failed categories without repeating the whole transfer.
