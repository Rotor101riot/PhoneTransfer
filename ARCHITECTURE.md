# PhoneTransfer — Architecture

## Transfer directions

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         PhoneTransfer (main.py)                         │
│                      PipelineManager.run()                              │
└─────────┬────────────────────────┬──────────────────────────────────────┘
          │                        │
          ▼                        ▼
  ┌───────────────┐       ┌────────────────┐
  │  iOS Source   │       │ Android Source │
  │  (libimobile  │       │ (ADB + companion│
  │   device /    │       │  APK port 7337)│
  │  backup2)     │       └───────┬────────┘
  └───────┬───────┘               │
          │                       │
          │   Extractors (dynamic import: extract_<category>_<platform>.py)
          │                       │
          ▼                       ▼
  ┌──────────────────────────────────────────────────────────┐
  │            Normalised in-memory records                  │
  │  Contact | SMSMessage | CallRecord | CalendarEvent |     │
  │  ReminderItem | MediaFile | NoteItem | BookmarkItem …    │
  └──────────────────────────────────────────────────────────┘
          │                       │
          ▼                       ▼
  ┌───────────────┐       ┌───────────────┐
  │  iOS Dest     │       │ Android Dest  │
  │               │       │               │
  │  BACKUP-MOD   │       │  CONTENT      │
  │  strategy:    │       │  PROVIDER     │
  │               │       │  (ADB shell   │
  │  1. Capture   │       │   content     │
  │     dest      │       │   insert)     │
  │     backup    │       │               │
  │  2. Decrypt   │       │  + rooted     │
  │  3. IOSBackup │       │    sqlite3    │
  │     Injector  │       │    fallback   │
  │  4. Repack    │       │               │
  │  5. Verify    │       │  Contacts:    │
  │  6. Restore   │       │  VCF push +   │
  │     (opt-in)  │       │  silent import│
  └───────────────┘       └───────────────┘
```

### Four paths in brief

| Source → Dest     | Extract path                                    | Inject path                                      |
|-------------------|-------------------------------------------------|--------------------------------------------------|
| iOS → iOS         | Backup (backup2) + decrypt (iphone_backup_decrypt) | Backup-mod: capture dest backup → inject → repack → restore |
| iOS → Android     | Backup (backup2) + decrypt                     | ADB content provider + rooted sqlite3 fallback   |
| Android → iOS     | ADB pull SQLite DBs via companion APK           | Backup-mod (same as iOS→iOS inject)              |
| Android → Android | ADB pull SQLite DBs via companion APK           | ADB content provider + rooted sqlite3 fallback   |

---

## Module map

```
main.py                      — entry point, logging setup, UAC elevation
│
├── core/
│   ├── pipeline_manager.py  — orchestrates extraction + injection, thread pool,
│   │                          multi-instance lock, SIGINT handler, iOS commit/verify/restore
│   ├── backup_manager.py    — iOS backup capture, decrypt, integrity check
│   ├── ios_backup_injector.py  — context manager: stage_db, commit(), RepackStats
│   ├── ios_backup_repacker.py  — packs modified DBs back into backup envelope
│   ├── ios_backup_verify.py    — FallbackDetector, take_baseline, verify_after_commit,
│   │                             PRAGMA integrity_check + plist round-trip + Manifest↔fs
│   │
│   ├── adb_manager.py       — ADB wrapper: shell, push, pull, pull_verified, shell_root
│   ├── companion_app_protocol.py  — TCP framing [uint32 LE len][UTF-8 JSON], v2 handshake,
│   │                               verify_companion_identity(), setup_adb_forward()
│   │
│   ├── normalization_schema.py — dataclasses: Contact, SMSMessage, CallRecord, …
│   ├── content_dedup.py     — DedupStore, SHA-256 dedup, versioned JSON envelope (v1)
│   ├── quirk_detector.py    — JSON-driven quirk matching against DeviceInfo
│   ├── pii_filter.py        — PiiRedactFilter (phone/email redaction on file handler)
│   ├── settings_manager.py  — Settings dataclass, load/save, singleton
│   ├── config_loader.py     — runtime config (paths, timeouts)
│   ├── device_connection_cache.py  — in-memory backup passwords, device pairing state
│   │
│   ├── extract_<category>_ios.py       (dynamically imported)
│   ├── extract_<category>_android.py   (dynamically imported)
│   ├── inject_<category>_ios.py        (dynamically imported)
│   └── inject_<category>_android.py    (dynamically imported)
│
├── ui/
│   ├── main_window.py       — CustomTkinter main window
│   ├── quirk_checklist_dialog.py  — pre-transfer QuirkChecklistDialog + RevertReminderDialog
│   └── …
│
└── reference/
    └── device_quirks.json   — quirk definitions loaded by quirk_detector.py
```

---

## Category responsibility matrix

| Category       | iOS Extract                        | iOS Inject (backup-mod)            | Android Extract              | Android Inject                                  | Lossiness notes                                          |
|----------------|------------------------------------|------------------------------------|------------------------------|-------------------------------------------------|----------------------------------------------------------|
| Contacts       | AddressBook.sqlitedb               | AddressBook.sqlitedb staged + injected | ADB pull contacts2.db        | Content provider insert + VCF push fallback     | Groups not restored on Android; org/note may be dropped by OEM |
| SMS / MMS      | sms.db                             | sms.db staged + injected           | ADB pull mmssms.db           | ADB shell `content insert` (default SMS role req'd on Android 10+) | MMS attachments may be absent if not in backup |
| Call log       | CallHistory.storedata              | CallHistory.storedata staged       | ADB pull calllog.db          | Content provider insert; rooted sqlite3 fallback on Android 10+ | VOIP calls (FaceTime, WhatsApp) not restored on dest    |
| Contacts groups| ABPersonFullTextSearch.sqlitedb + group tables | Group tables staged           | N/A (Android flat contacts)  | N/A                                             | iOS groups lost on Android→iOS if source was Android    |
| Calendar       | Calendar.sqlitedb                  | Calendar.sqlitedb staged           | ADB pull calendar.db         | Content provider insert                         | Recurring rule fidelity varies by calendar app          |
| Reminders      | CloudKit/Reminders DB              | Reminders DB staged                | ADB pull (if supported)      | Content provider insert                         | iOS 16+ reminder subtasks not representable on Android  |
| Photos / Video | CameraRollDomain DCIM              | CameraRollDomain DCIM injected     | ADB pull DCIM                | ADB push to DCIM; MediaScanner broadcast        | EXIF preserved; Live Photos become JPEG+MOV pair on Android |
| Notes          | NoteStore.sqlite                   | NoteStore.sqlite staged            | ADB pull notes DB (Samsung/AOSP) | Content provider or file push                | Rich-text formatting (tables, sketches) lost on Android |
| Bookmarks      | SafariTabs / Bookmarks.db          | Bookmarks.db staged                | ADB pull browser DBs         | Browser-specific content provider              | Cross-browser bookmark format may differ                |
| WhatsApp       | AppDomain backup (if present)      | Staged into AppDomain              | ADB pull (root or companion) | File push + key restore                         | E2E encrypted; requires matching key — experimental     |
| Signal         | AppDomain backup (if present)      | Staged into AppDomain              | ADB pull (root or companion) | File push                                       | Signal uses sealed-sender; cross-device restore fragile |
| Health         | Health/healthdb.sqlite             | healthdb.sqlite staged             | N/A (no standard DB)         | N/A                                             | iOS-only; Android has no equivalent restore path        |
| App list       | iTunes metadata                    | N/A (can't install apps)           | ADB `pm list packages`       | N/A                                             | Informational only — apps not transferred                |
| WiFi passwords | keychain (encrypted)               | N/A (not extractable without jailbreak) | ADB root pull wpa_supplicant | N/A                                            | iOS: unavailable without jailbreak                      |

---

## iOS backup-mod pipeline (detailed)

```
ensure_backup_for_transfer()
  └─ backup2 backup → decrypt (iphone_backup_decrypt) → integrity_check (PRAGMA + plist)

_open_dest_ios_backup_injector()
  ├─ capture dest backup (backup2 backup)
  ├─ _hardlink_backup() → tmp/ios_original/<udid>/   (zero-cost snapshot)
  └─ IOSBackupInjector(dest_backup_path).__enter__()

For each category:
  extractor.extract(source) → list[NormalizedRecord]
  injector.stage_db(domain, filename) → sqlite3.Connection
  inject_<category>_ios(conn, records)

IOSBackupInjector.commit()
  └─ IOSBackupRepacker.repack() → tmp/ios_repacked/<udid>/

ios_backup_verify.verify_after_commit()
  ├─ PRAGMA integrity_check on every .db in repacked dir
  ├─ plist round-trip validation on every .plist
  └─ Manifest.db ↔ filesystem coherence check

if ios_auto_restore_modified_backup:
  backup2 restore tmp/ios_repacked/<udid>/
else:
  log path for manual restore via iMazing / pymobiledevice3
```

---

## Companion APK protocol (Android source/dest)

```
TCP framing:  [uint32 LE message length][UTF-8 JSON payload]

Handshake (v2):
  → {"type": "hello", "version": 2}
  ← {"type": "hello_ack", "version": 2, "min_version": 1}

Identity check (before forward):
  adb shell dumpsys package com.phonetransfer.companion  → pkg UID
  adb shell cat /proc/net/tcp6                           → port owner UID
  mismatch → reject connection (prevents port-squatting)

ADB forward:  adb forward tcp:7337 tcp:7337
```
