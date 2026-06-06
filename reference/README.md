# reference/ — Developer Reference Assets

Static look-up tables, schema captures, and reverse-engineering notes used
by the PhoneTransfer runtime and build tooling.  None of these files are
written at runtime — they are read-only reference data.

---

## Files

### `device_quirks.json`
**Used by:** `core/quirk_detector.py`, displayed in `ui/quirk_checklist_dialog.py`

JSON array of device compatibility quirks.  Each entry has match criteria
(platform, brand, OS version range) and step-by-step instructions shown
in the pre-transfer checklist dialog.  23 entries as of 2026-06-05 covering:
- Android USB debugging and trust flows
- iOS trust / restricted-mode / developer-mode / backup-encryption
- Samsung (PTP/Knox), Xiaomi (MIUI/SIM), Huawei (HDB), OnePlus, Motorola, Sony
- Battery level, MDM/enterprise, OEM unlock note
- iOS schema-drift warnings (iOS 16–18 source/destination version mismatches)

To add a quirk: append an entry to the `"quirks"` array and restart — the
detector loads the file at startup.  See `core/quirk_detector.py` for the
full match-criteria schema.

---

### `ios_backup_schema.json`
**Used by:** `core/backup_manager_ios.py`, extractors

SHA-1 backup file hashes and SQLite schema reference for every iOS domain
database extracted by PhoneTransfer.  Sourced from The iPhone Wiki,
kacos2000/queries, MVT project, and richinfante.com reverse engineering.

Covers: AddressBook, SMS/iMessage, call history (pre-iOS-13 and iOS 8+
Core Data paths), calendar, notes, voicemail, health, photos, reminders,
Safari bookmarks, and several others.  Use the `"hash"` values to locate
files inside an iTunes/Finder backup directory.

---

### `android_schema.json`
**Used by:** Android extractors

ContentProvider URI, required ADB permissions, and column schemas for every
Android data source accessed by PhoneTransfer.  Covers Android 5–15 (AOSP).

Sources: AOSP source, MVT (Amnesty International), kacos2000/queries.

Note: Samsung One UI, Xiaomi MIUI, and OPPO ColorOS forks add OEM columns.
Use `PRAGMA table_info` (SQLite path) or `ContentResolver.query(projection=null)`
(ContentProvider path) to discover extras at runtime.

---

### `android_device_lookup.json`
**Used by:** `reference/device_names.py`, `reference/enrich_device_lookups.py`

Large (~1.3 MB) mapping of Android device model identifiers (`model` field from
`adb shell getprop ro.product.model`) to human-readable marketing names.
Updated by `enrich_device_lookups.py` on each startup (pulls from a bundled
offline snapshot; no network required).

---

### `device_lookup.json`
**Used by:** `reference/device_names.py`

Mapping of iOS device identifiers (e.g. `iPhone15,2`) to marketing names
(e.g. "iPhone 14 Pro").  Sourced from TheAppleWiki / ipsw.me device list.
Updated by `enrich_device_lookups.py`.

---

### `cpu_lookup.json`
**Used by:** `reference/device_names.py`

Maps iOS SoC identifiers (e.g. `T8120`) to chip names (e.g. "A16 Bionic").
Used to populate the device info panel in the UI.

---

### `dual_sim_models.json`
**Used by:** device detection

List of iPhone model identifiers that support Dual SIM (nano-SIM + eSIM or
nano-SIM + nano-SIM).  Used to decide whether to show a SIM-slot selector
during device pairing.

---

### `esim_only_models.json`
**Used by:** device detection

List of iPhone model identifiers that are eSIM-only (no physical SIM tray).
Used to skip SIM-related steps in the companion pairing flow.

---

### `hardware_reference.json`
**Used by:** `core/prerequisite_checker.py` (driver checks)

Small reference table of USB vendor IDs and known driver names for iOS and
Android devices.  Used to validate that the correct USB driver is installed
on the host before starting a transfer.

---

### `device_names.py`
**Used by:** UI device panels, logging

Python module that wraps the JSON lookup files above.  Exports:
- `friendly_name(identifier) -> str` — resolves iOS/Android model ID to a display name
- `refresh_caches()` — re-reads all JSON files after `enrich_all()` runs

---

### `enrich_device_lookups.py`
**Used by:** `main.py` startup

Merges any newly bundled device data into `android_device_lookup.json` and
`device_lookup.json`.  Runs at startup; the files are updated in-place.
Safe to run repeatedly (idempotent merge).

---

### `telephony/`

Offline copy of the Android `Telephony.Mms` API reference page (HTML + assets),
saved for offline consultation during MMS ContentProvider development.  Not
used at runtime — developer reference only.

---

## Source provenance

| File                        | Primary sources                                               |
|-----------------------------|---------------------------------------------------------------|
| `ios_backup_schema.json`    | The iPhone Wiki, kacos2000/queries, MVT, richinfante.com      |
| `android_schema.json`       | AOSP source, MVT, kacos2000/queries, XDA                     |
| `android_device_lookup.json`| Bundled offline snapshot (gsmarena / imei.info cross-ref)    |
| `device_lookup.json`        | TheAppleWiki, ipsw.me                                         |
| `device_quirks.json`        | libimobiledevice/issues, pymobiledevice3/issues, XDA, testing|
| `cpu_lookup.json`           | TheAppleWiki SoC table                                        |
| `dual_sim_models.json`      | Apple tech specs pages                                        |
| `esim_only_models.json`     | Apple tech specs pages                                        |
| `hardware_reference.json`   | USB-IF vendor ID registry, OEM driver pages                   |
