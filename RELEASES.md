# Releases

## v1.0.0 — 2026-06-06

Initial public release.

### What's included

- **14 transfer categories:** contacts, contact groups, blocked numbers, SMS/MMS,
  call history, voicemail, photos, videos, ringtones, voice memos, wallpaper,
  calendar, reminders, notes, alarms, bookmarks, browser history, clipboard,
  installed apps, WhatsApp, Signal, mail accounts.

- **All four directions:** Android → Android, Android → iPhone,
  iPhone → Android, iPhone → iPhone.

- **iOS destination strategy:** backup-mod pipeline — captures an encrypted
  destination backup, injects data, verifies structural integrity (SQLite
  PRAGMA, plist round-trip, Manifest↔filesystem coherence), and optionally
  restores automatically.

- **Android companion app** (`companion/`) — socket-based extraction and
  injection for all 14 categories; sideloaded automatically via ADB.

- **Per-category timeout enforcement** — `send_recv_timed()` prevents stalled
  transfers from blocking indefinitely.

- **AMR-WB → AMR-NB voicemail normalization** — transparently transcodes
  carrier voicemails that Android cannot play back natively.

- **PyInstaller optional build** — `build.ps1` produces a standalone
  `dist/PhoneTransfer/` folder for users who prefer not to run from source.

### Known gaps

- `inject_apps_ios`, clipboard, WhatsApp, Signal, Health — require
  jailbreak/root; partial support only on stock devices.
- Android voicemail (Visual Voicemail) — carrier-specific, not yet implemented.
- Real-device backup restore smoke test — pipeline is dry-run verified;
  in-vivo restore on a physical iPhone is pending.
- PyInstaller exe build is optional and not code-signed (SmartScreen warning
  expected on first run).
