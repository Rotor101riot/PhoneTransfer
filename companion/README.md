# PhoneTransfer Companion

Android companion app for [PhoneTransfer](../README.md). Lives in `companion/`
inside the main monorepo — not a standalone project.

The pre-built APK (`../assets/companion.apk`) is bundled with the desktop app
and sideloaded automatically via ADB. You only need to build from source here
if you're modifying the companion or building a fork.

## Build from source

1. Open this `companion/` folder in Android Studio (File → Open).
2. Android Studio will sync Gradle automatically.
3. Grant all permissions when prompted on-device.

> **Note:** `gradle/wrapper/gradle-wrapper.jar` is not included in source.
> Android Studio downloads it automatically on first sync.
> Alternatively, run `gradle wrapper --gradle-version 8.6` if you have Gradle installed.

## How it works

1. The PhoneTransfer desktop app sideloads this APK via ADB:
   ```
   adb install PhoneTransferCompanion.apk
   adb shell am start -n com.phonetransfer.companion/.MainActivity
   adb forward tcp:7337 tcp:7337
   ```
2. The APK starts a socket server on port 7337.
3. The desktop app connects and issues JSON commands over the socket.
4. The APK reads/writes contacts, SMS, calls, calendar, alarms, bookmarks, notes, and reminders using Android ContentResolver APIs.
5. Media files (photos, videos, ringtones, voice memos) are transferred by the desktop app directly via `adb pull`/`adb push` — the APK only provides file path lists.

## Protocol

Each message (both directions) is a **length-prefixed JSON frame**:
```
[4 bytes: uint32 little-endian payload length][UTF-8 JSON string]
```

### Commands (PC → APK)
| cmd | description |
|-----|-------------|
| `ping` | Health check |
| `capabilities` | Returns supported categories and root status |
| `extract` | Extract structured data for a category |
| `inject` | Inject structured data for a category |
| `media_list` | List media file paths (photos/videos/etc.) |
| `root_exec` | Execute approved shell command via su |
| `stop` | Gracefully shut down the socket server |

### Categories supported
`contacts`, `sms`, `calls`, `calendar`, `alarms`, `blocked`, `bookmarks`, `notes`, `reminders`

### Media (path-list only)
`photos`, `videos`, `ringtones`, `voice_memos`

## Permissions required
- `READ_CONTACTS` / `WRITE_CONTACTS`
- `READ_CALL_LOG` / `WRITE_CALL_LOG`
- `READ_SMS`
- `READ_CALENDAR` / `WRITE_CALENDAR`
- `READ_MEDIA_IMAGES` / `READ_MEDIA_VIDEO` / `READ_MEDIA_AUDIO` (API 33+)
- `READ_EXTERNAL_STORAGE` (API < 33)
- `FOREGROUND_SERVICE` / `FOREGROUND_SERVICE_DATA_SYNC`
- `POST_NOTIFICATIONS`

## Distribution

**This APK cannot be published on Google Play.**

Two permissions declared in `AndroidManifest.xml` are on Google's restricted
permissions list:

| Permission | Policy category | Why it is needed |
|---|---|---|
| `MANAGE_EXTERNAL_STORAGE` | Personal and Sensitive Information | Writing restore data to arbitrary shared-storage paths (`/sdcard/DCIM`, `/sdcard/Ringtones`, etc.) on Android 11+ |
| `QUERY_ALL_PACKAGES` | Device and Network Abuse | Enumerating installed apps for APK backup/restore |

Google requires a Declaration Form for each and routinely denies approval for
apps that are not dedicated file managers or device-backup utilities.  Even if
approved, the review process can take weeks and approval may be revoked on
policy change.

**Distribution path:** sideload-only via ADB install.  The PhoneTransfer
desktop app handles installation automatically:
```
adb install -r PhoneTransferCompanion.apk
```

If you fork this project and want Play Store distribution, you must replace
both restricted permissions with scoped alternatives:
- `MANAGE_EXTERNAL_STORAGE` → `MediaStore` APIs + `SAF` (Storage Access
  Framework) for paths outside `MediaStore` scope.
- `QUERY_ALL_PACKAGES` → declare `<queries>` elements for the specific package
  names / intents you need to resolve (Android 11+ visibility filtering).
