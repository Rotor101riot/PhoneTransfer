# Releases

---

## v1.1.0 — 2026-06-08

### Added

- **Selective categories** — 5 previously hidden categories (Contact Groups, Voicemail, Browser History, Clipboard, Mail Accounts) are now visible and toggleable in the UI. All 22 pipeline categories are now exposed.
- **Structured transfer log** — post-transfer summary now shows a formatted table with extracted vs. injected counts per category, dropped-record warnings, and a total row. A plain-text copy is written to `~/Documents/PhoneTransfer/logs/` after every run.
- **Dry-run presentation** — the UI now clearly distinguishes a dry-run result from a real transfer: labels, stats bar, toast, and log file all read "DRY RUN PREVIEW — no data was written."
- **Compatibility matrix** in README — per-category, per-direction support table covering all 22 categories and all four transfer directions.
- **Known lossiness table** in README — documents exactly what data survives transfer but is not identical to the original (formatting, read state, thread structure, audio quality, etc.).
- **AI & Data Sovereignty disclosure** — opening statement in README and SECURITY.md explaining the role of AI in development and the complete absence of any network component at runtime.
- **ROADMAP.md** — honest, prioritised development backlog separated by what is blocking each item (hardware, design decisions, or code).

---

## v1.0.0 — 2026-06-06

Initial public release.

### What's included

- **22 transfer categories:** contacts, contact groups, blocked numbers, SMS/MMS,
  call history, voicemail, photos, videos, ringtones, voice memos, wallpaper,
  calendar, reminders, notes, alarms, bookmarks, browser history, clipboard,
  apps, WhatsApp, Signal, mail accounts.

- **All four directions:** Android → Android, Android → iPhone,
  iPhone → Android, iPhone → iPhone.

- **iOS destination strategy:** backup-mod pipeline — captures an encrypted
  destination backup, injects data, verifies structural integrity (SQLite
  PRAGMA, plist round-trip, Manifest↔filesystem coherence), and optionally
  restores automatically.

- **Android companion app** (`companion/`) — socket-based extraction and
  injection; sideloaded automatically via ADB.

- **Per-category timeout enforcement** — `send_recv_timed()` prevents stalled
  transfers from blocking indefinitely.

- **AMR-WB → AMR-NB voicemail normalization** — transparently transcodes
  carrier voicemails that Android cannot play back natively.

- **PyInstaller optional build** — `build.ps1` produces a standalone
  `dist/PhoneTransfer/` folder for users who prefer not to run from source.

### Known gaps at release

- WhatsApp, Signal, Health — require jailbreak/root; partial support only on stock devices.
- Android voicemail (Visual Voicemail) — carrier-specific, not yet implemented.
- Real-device backup restore smoke test — pipeline is dry-run verified; in-vivo restore on a physical iPhone is pending.
- iOS 18+ schema changes not yet surveyed.
- PyInstaller exe build is optional and not code-signed (SmartScreen warning expected on first run).
