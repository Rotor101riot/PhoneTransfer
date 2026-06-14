# PhoneTransfer

Transfer contacts, SMS, call logs, photos, videos, notes, calendar, and more between Android and iOS devices — in all four directions, entirely on your own hardware.

---

## AI & Data Sovereignty

**This project was built with AI assistance (Claude by Anthropic).** That fact is disclosed here, first, because transparency about how software is made is part of respecting the people who use it.

What the AI did: helped write, review, and refine the code.
What the AI does at runtime: **nothing.** It has no presence in the tool, no access to your data, and no network role of any kind.

**Why this tool exists:**
Your contacts, messages, photos, and call history belong to you — not to Apple, Google, or any transfer service that routes your data through its own servers. PhoneTransfer runs entirely locally. Nothing leaves your machine. No account required. No telemetry. No subscription.

This is data sovereignty in practice: the right to move your own data, between your own devices, without asking permission.

---

## Transfer directions

| Source | Destination | Method |
|--------|-------------|--------|
| Android | Android | Companion app + ADB |
| Android | iPhone | Backup-mod: inject into destination backup, restore |
| iPhone | Android | Decrypt backup → stream to companion app |
| iPhone | iPhone | Backup capture → inject → restore (additive merge) |

---

## Compatibility matrix

| Category | A → A | A → i | i → A | i → i |
|---|:---:|:---:|:---:|:---:|
| Contacts | ✓ | ✓ | ✓ | ✓ |
| Contact Groups | ✓ | ✓ | ✓ | ✓ |
| Blocked Numbers | ✓ | ⚠ | ⚠ | ✓ |
| Messages / SMS | ✓ | ✓ | ✓ | ✓ |
| Call Log | ✓ | ✓ | ✓ | ✓ |
| Voicemail | ⚠ | ⚠ | ✓ | ✓ |
| Photos | ✓ | ✓ | ✓ | ✓ |
| Videos | ✓ | ✓ | ✓ | ✓ |
| Ringtones | ✓ | ✓ | ✓ | ✓ |
| Voice Memos | ✓ | ✓ | ✓ | ✓ |
| Wallpaper | ✓ | ✓ | ✓ | ✓ |
| Calendar | ✓ | ✓ | ✓ | ✓ |
| Reminders | ✓ | ✓ | ✓ | ✓ |
| Notes | ✓ | ✓ | ✓ | ✓ |
| Alarms | ✓ | ✓ | ✓ | ✓ |
| Bookmarks | ✓ | ✓ | ✓ | ✓ |
| Browser History | ✓ | ✓ | ✓ | ✓ |
| Clipboard | ✓ | ✓ | ✓ | ✓ |
| Apps | ⚠ | ✗ | ✗ | ✗ |
| WhatsApp | ⚠ | ⚠ | ⚠ | ⚠ |
| Signal | ⚠ | ⚠ | ⚠ | ⚠ |
| Mail Accounts | ✓ | ✓ | ✓ | ✓ |

**Key:** ✓ full support · ⚠ partial or conditional · ✗ not supported

- **Apps:** Android → Android only, and only with root for full APK extraction. Install-list only on stock devices.
- **WhatsApp / Signal:** Message history requires root (Android) or jailbreak (iOS). Media attachments only on stock devices.
- **Blocked Numbers:** iOS → iOS and Android → Android only; cross-platform blocked number transfer is carrier-dependent.
- **Voicemail:** iOS full support. Android requires Visual Voicemail — carrier-specific; not available on all carriers.

---

## Known lossiness

Some data survives transfer but is not identical to the original. These are known, documented trade-offs — not bugs:

| Category | What is lost |
|---|---|
| Messages / SMS | Read/unread state lost on Android → iOS. Thread timestamps approximate. MMS group thread structure may flatten. |
| Notes | Rich formatting (tables, drawings, sketches, attachments) not preserved. Plain text only. |
| Calendar | Custom alarm sounds lose their sound; alarm timing is preserved. |
| Reminders | Subtask hierarchy flattened on Android destination. |
| Contacts | Linked accounts (Google, iCloud source) are unlinked on destination. Contact photos are preserved. |
| Voicemail | AMR-WB transcoded to AMR-NB for Android compatibility; minor audio quality reduction. |
| WhatsApp / Signal | Starred messages, reactions, and disappearing message settings are not transferred. |

The transfer log written after each run records extracted vs. injected counts per category. Any gap between those numbers indicates records that could not be written on the destination.

---

## Prerequisites

- **Python 3.10+**
- **ADB** (Android Debug Bridge) — included in `bin/platform-tools/`
- **libimobiledevice** — included in `bin/libimobiledevice/`
- **pymobiledevice3** — installed via `requirements.txt`
- Android: USB debugging enabled; iOS: device trusted (paired)

---

## Install

**Quick start (Windows, recommended):**
```
setup-deps.bat
python main.py
```
Or from PowerShell:
```powershell
.\setup-deps.ps1
python main.py
```

> `setup-deps.bat` calls three scripts in `scripts/` — each is independently runnable.  Output streams through so you see every package as it installs.  The three-step approach skips `pylzss` and `lzfse` — transitive C-extension deps of `pymobiledevice3` with no pre-built wheels for Python 3.13+ — which PhoneTransfer never needs (they only serve IPSW firmware handling).

**Manual install (all platforms):**
```bash
pip install --no-deps pyimg4
pip install --no-deps -r requirements.txt
pip install -r requirements-safe.txt
python main.py
```

`requirements-safe.txt` is generated from `requirements-lock.txt` with `pylzss`, `lzfse`, and `pillow-heif` excluded.  If you upgrade any pinned versions, regenerate it by copying the lock file and removing those three lines.

**Optional — HEIC/HEIF photo conversion:**
```batch
pip install pillow-heif
```
Without it, HEIC photos push as originals — Android 13+ handles them natively.

## Optional: standalone exe (Windows)

A PyInstaller build is available for users who prefer a single distributable folder rather than a Python environment. It requires code-signing to avoid Windows SmartScreen warnings and may trigger AV heuristics on first run — source install is simpler for most users.

```powershell
.\build.ps1          # build + smoke test
.\build.ps1 -Clean   # clean rebuild
```

Output: `dist\PhoneTransfer\PhoneTransfer.exe`

---

## Using the app

The UI will detect connected devices automatically. Select source and destination, choose categories, and click **Transfer**.

**Dry run:** check the "Dry run" box to preview what would be transferred without writing anything. A structured summary is shown in the log panel and saved to `~/Documents/PhoneTransfer/logs/`.

**Selective categories:** enable or disable individual categories before starting. Greyed-out categories are not supported for the selected device pair.

### iOS destination (backup-mod strategy)

For Android → iPhone and iPhone → iPhone transfers, PhoneTransfer:

1. Captures a full encrypted backup of the **destination** iPhone
2. Injects the transferred data into the backup
3. Verifies the repack (PRAGMA integrity, plist round-trip, Manifest coherence)
4. Optionally restores the modified backup to the device

When `ios_auto_restore_modified_backup` is enabled (Settings → Transfer), the restore happens automatically. Otherwise the repacked backup is left at `temp/ios_repacked/<udid>/` for manual restore via Finder/iTunes.

> **Note:** The backup → restore strategy has been dry-run verified against real encrypted backups but not yet smoke-tested against a real device restore. Treat the auto-restore as experimental until that validation is complete.

---

## Security notes

- Backup passwords are held in memory for the duration of the session and not written to disk unless already present in `settings.json`.
- Log files may contain phone numbers and contact names. Do not share logs publicly.
- The companion APK is sideloaded via ADB — it is not distributed via the Play Store.
- No data is transmitted to any server. There is no network component beyond the local ADB / companion socket on port 7337.

See [SECURITY.md](SECURITY.md) for the full security policy and vulnerability reporting contact.

---

## Transfer guides

Step-by-step walkthroughs for each transfer mode — prerequisites, exact steps, what to expect, and common fixes:

See [GUIDES.md](GUIDES.md)

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the prioritised development backlog.

---

## License

GPL v2 — see [LICENSE](LICENSE).

This project includes vendored copies of libimobiledevice, libplist, and ideviceinstaller (all GPL v2). See [vendor/NOTICES.md](vendor/NOTICES.md) for full attribution.
