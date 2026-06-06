# iOS Version Compatibility

This document covers which iOS versions are tested, known schema changes that
affect extraction and injection, and recommended workarounds when a category
does not transfer as expected.

---

## Tested configurations

| Source iOS | Destination iOS | Status         | Notes                                          |
|------------|-----------------|----------------|------------------------------------------------|
| 14.x       | 14.x            | Verified       | Reference baseline for all extractors          |
| 15.x       | 14.x / 15.x     | Verified       | No schema breakage vs 14                       |
| 16.x       | 15.x / 16.x     | Verified       | Reminders subtasks present but not restored    |
| 17.x       | 16.x / 17.x     | Verified       | ABMultiValue.uid absent; phone order may differ|
| 18.x       | 17.x / 18.x     | Partial        | Some backup domain schemas tightened; see below|
| 18.x       | Android 10–14   | Partial        | Same extraction caveats as iOS 18 source       |
| Android    | 14.x – 18.x     | Verified       | Backup-mod inject path; no iOS-version issues  |

"Verified" means a real-device end-to-end run with `dry_run_pipeline.py` produced
a non-empty transfer for contacts, SMS, call log, and calendar.  Other categories
were checked by log inspection.

---

## Schema drift by iOS version

### iOS 14 — baseline

- **AddressBook.sqlitedb** — `ABPerson`, `ABMultiValue` (includes `uid` column),
  `ABMultiValueEntry`, `ABMultiValueLabel`.
- **sms.db** — `message`, `chat`, `attachment` tables stable since iOS 9.
- **Calendar.sqlitedb** — `ZCALCALENDARITEM`, `ZCALATTENDEE`, `ZCALALARM` stable.
- **Reminders** — `ZREMCDREMINDER`, `ZREMCDFOLDER`; no subtasks.

### iOS 15

- No breaking schema changes vs iOS 14 for any supported category.
- Reminders gained `ZTITLE2` (shared lists) — ignored by extractor, not lossy.

### iOS 16

- **Reminders** — added `ZPARENTREMINDER` (subtask parent link) and `ZTAGS` blob.
  Subtasks are extracted as flat top-level reminders on Android destinations.
  iOS → iOS backup-mod preserves the full subtask tree.
- **CallHistory.storedata** — added `ZDURATION_ISO8601` alongside the existing
  `ZDURATION` integer; extractor reads integer column, no impact.

### iOS 17

- **AddressBook.sqlitedb** — `ABMultiValue.uid` column removed.  The extractor
  uses `PRAGMA table_info` to detect its presence and falls back to row-order for
  phone/email sorting.  Contact data is complete; preferred-number ordering may
  not be preserved in cross-device transfers.
- **Notes** — `ZNOTEDATA` blob encoding changed in some builds.  Notes extractor
  reads `ZSNIPPET` (plain-text fallback) when the primary blob cannot be decoded.
- **Health** — `healthdb.sqlite` schema gained several new tables; the health
  extractor is read-only (metrics only) and skips unknown tables safely.

### iOS 18

- **Backup domain schemas** — Apple tightened encryption schemas for the
  `AppDomainGroup-group.com.apple.contacts` and
  `AppDomainGroup-group.com.apple.reminders` domains in some iOS 18 builds.
  Contacts and Reminders may extract as empty even when the backup decrypts
  without error.  Check `tmp/logs/phonetransfer.log` for extractor warnings.
- **Workaround (contacts)** — export contacts as `.vcf` from the Contacts app
  and import on the destination manually.
- **Workaround (reminders)** — export from Reminders via Share → as Reminders
  File (`.ics`) and import on the destination.
- All other categories (SMS, call log, calendar, photos) are unaffected on iOS 18.

---

## Cross-version inject constraints

When the **destination** iOS version is older than the source, the backup-mod
injector only writes columns that exist in the destination's schema
(detected via `PRAGMA table_info` on each staged database).  Unknown columns
are silently dropped.  The most common lossy cases:

| Data                        | Source → Dest           | What is lost                       |
|-----------------------------|-------------------------|------------------------------------|
| Reminder subtasks           | iOS 16+ → iOS 15 or older | Subtasks flattened to top-level  |
| Reminder tags               | iOS 16+ → iOS 15 or older | Tags dropped                     |
| Contact preferred phone     | iOS 17+ → iOS 16 or older | Ordering may differ              |
| SharePlay / FaceTime links  | Any iOS → Any iOS       | Not extracted (iCloud only)        |
| iMessage reactions          | Any iOS → Android       | Dropped (no Android equivalent)    |

---

## iphone_backup_decrypt library notes

- Library version ≥ 0.14.0 required for iOS 16+ keybag format.
- `_decrypt_inner_file` size-check is monkey-patched to skip enforcement
  (see `core/backup_manager_ios.py`); the library's default raises on files
  whose decrypted size doesn't match the backup manifest exactly, which
  is a false-positive on some iOS 18 backups.
- The `Manifest.db` connection must not be closed before all domain databases
  are extracted; closing it early causes `sqlite3.ProgrammingError` on the
  next `stage_db` call.

---

## iOS 18 / Sequoia — detailed findings

### Backup encryption domain changes

iOS 18 (released alongside macOS Sequoia) tightened the backup encryption
model for several domains.  The changes that affect PhoneTransfer:

| Domain | Change | Impact |
|--------|--------|--------|
| `AppDomainGroup-group.com.apple.contacts` | Encryption key derivation changed for some keybag protection classes | Contacts may decrypt to an empty or partial database on some iOS 18.0–18.1 builds |
| `AppDomainGroup-group.com.apple.reminders` | `ZREMCDREMINDER` columns shifted; `ZREMCDFOLDER` gained a new synthetic-key column | Reminders extract as empty if the extractor doesn't detect the new column layout |
| `HomeDomain/Library/SMS/sms.db` | No change — schema stable | Unaffected |
| `WirelessDomain/CallHistory.storedata` | No change — Core Data store format stable | Unaffected |
| `CameraRollDomain` | No change — DCIM structure unchanged | Unaffected |

### The `_decrypt_inner_file` size-check false positive

`iphone_backup_decrypt` ≥ 0.14.0 validates that the decrypted file size
matches the size recorded in `Manifest.db`.  On some iOS 18.0 builds, the
manifest records a size that is off by a small alignment padding introduced
by the new keybag derivation path.  This causes the library to raise on
legitimately-decrypted files.

**Fix applied:** `core/backup_manager_ios.py` monkey-patches
`_decrypt_inner_file` to skip the size check.  The decrypted content is
still written and verified by the extractor's own `PRAGMA integrity_check`.

### Keychain changes (iOS 18 / Sequoia)

iOS 18 aligned the local keychain backup format with iCloud Keychain's
sealed-sender model.  This affects **keychain items only** — passwords,
certificates, and Wi-Fi credentials stored in Keychain.  PhoneTransfer
does not extract keychain items (they require a jailbreak or a known
backup password + specific keybag class keys), so this change has no
impact on supported categories.

If a future version adds keychain extraction, the relevant change is in
the `BackupKeyBag` protection class 9 and 11 key derivation — iOS 18
uses a different PBKDF2 iteration count than iOS 17.

### Stage Manager / scene-based state (iOS 18 iPadOS only)

iPadOS 18 with Stage Manager enabled stores per-scene window state in a
new `SceneStorage` domain.  This domain is not extracted by PhoneTransfer
and is silently skipped.  It contains UI layout state, not user data.

### Recommended workarounds for iOS 18 source devices

1. **Contacts empty:** Export from the Contacts app as `.vcf` → import manually on destination.
2. **Reminders empty:** Share → Export as Reminders File (`.ics`) → import on destination.
3. **All other categories:** No workaround needed — transfers correctly.
4. **File a bug:** Attach `tmp/logs/phonetransfer.log` with your iOS 18 minor version noted.

---

## How to file a schema-drift bug

1. Run the transfer.
2. Attach `tmp/logs/phonetransfer.log` (phone numbers and email addresses are
   automatically redacted).
3. Include: source iOS version, destination platform and version, which
   categories were empty or incomplete.
4. File at: https://github.com/<your-repo>/PhoneTransfer/issues
