# PhoneTransfer

Transfer contacts, SMS, call logs, photos, videos, notes, calendar, and more between Android and iOS devices — in all four directions.

## Transfer directions

| Source | Destination | Method |
|--------|------------|--------|
| Android | Android | Companion app + ADB |
| Android | iPhone | Backup-mod: inject into destination backup, restore |
| iPhone | Android | Decrypt backup → stream to companion app |
| iPhone | iPhone | Backup capture → inject → restore (additive merge) |

**Supported categories:** contacts, contact groups, blocked numbers, SMS/MMS, call history, voicemail, photos, videos, ringtones, voice memos, wallpaper, calendar, reminders, notes, alarms, bookmarks, browser history, clipboard, installed apps, WhatsApp, Signal, mail accounts.

## Prerequisites

- **Python 3.10+**
- **ADB** (Android Debug Bridge) — included in `bin/platform-tools/`
- **libimobiledevice** — included in `bin/libimobiledevice/`
- **pymobiledevice3** — installed via `requirements.txt`
- Android: USB debugging enabled; iOS: device trusted (paired)

## Install

```
pip install -r requirements.txt
python main.py
```

On Windows with encrypted iOS backups, `sqlcipher3` requires the SQLCipher DLL (see `dependencies/`).

This is the primary and recommended way to run PhoneTransfer. It keeps the code auditable and avoids antivirus friction.

## Optional: standalone exe (Windows)

A PyInstaller build is available for users who prefer a single distributable folder rather than a Python environment. It requires code-signing to avoid Windows SmartScreen warnings and may trigger AV heuristics on first run — source install is simpler for most users.

```powershell
.\build.ps1          # build + smoke test
.\build.ps1 -Clean   # clean rebuild
```

Output: `dist\PhoneTransfer\PhoneTransfer.exe`

The UI will detect connected devices automatically. Select source and destination, choose categories, and click **Transfer**.

### iOS destination (backup-mod strategy)

For Android → iPhone and iPhone → iPhone transfers, PhoneTransfer:

1. Captures a full encrypted backup of the **destination** iPhone
2. Injects the transferred data into the backup
3. Verifies the repack (PRAGMA integrity, plist round-trip, Manifest coherence)
4. Optionally restores the modified backup to the device

When `ios_auto_restore_modified_backup` is enabled (Settings → Transfer), the restore happens automatically. Otherwise the repacked backup is left at `temp/ios_repacked/<udid>/` for manual restore via Finder/iTunes.

> **Note:** The backup → restore strategy has been dry-run verified against real encrypted backups but not yet smoke-tested against a real device restore. Treat the auto-restore as experimental.

## Limitations

- **Apps, clipboard, WhatsApp, Signal, Health** data require a jailbroken/rooted device for full extraction; partial support only on stock devices.
- **Voicemail** on Android is carrier-specific (Visual Voicemail) and may not be extractable.
- iOS 18+ schema changes are not yet surveyed.

## Security notes

- Backup passwords are held in memory for the duration of the session and not written to disk unless already present in `settings.json`.
- Log files may contain phone numbers and contact names. Do not share logs publicly.
- The companion APK is sideloaded via ADB and requires `adb install` or USB installation — it is not distributed via the Play Store.

## License

GPL v2 — see [LICENSE](LICENSE).

This project includes vendored copies of libimobiledevice, libplist, and ideviceinstaller (all GPL v2). See [vendor/NOTICES.md](vendor/NOTICES.md) for full attribution.
