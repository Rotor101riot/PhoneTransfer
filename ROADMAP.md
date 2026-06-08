# Roadmap

This document reflects honest priorities, not aspirational feature lists. Items are separated by what is blocking them — hardware access, design decisions, or just code.

---

## Now — unblocked, high impact

These are the most important gaps. None require new features; they require validation and honesty about current state.

- **Real-device restore smoke test** — run the full pipeline against a spare iPhone across iOS 16, 17, and 18+. Document exactly what survives per category. Until this is done, the auto-restore option should remain clearly marked experimental.
- **iOS 18+ schema survey** — Apple restructured `sms.db`, `NoteStore.sqlite`, and the Reminders backend in iOS 18. The current injectors may silently write nothing or corrupt data on 18+ destinations. Needs to be mapped before any iOS 18 destination is considered supported.
- **Lossiness warnings surfaced before transfer** — the known lossiness table in README.md should also appear in the UI as a pre-flight warning for affected category + direction combinations, not just in the post-transfer log.
- **GitHub Issues from audit punchlist** — the 58-item internal audit exists; converting it to tracked Issues makes it visible and actionable for anyone who wants to contribute.

---

## Next — code-ready, moderate effort

These are well-understood problems with known solution paths. No design unknowns.

- **Stock-device WhatsApp path** — use the Android local backup (no root required) combined with the iTunes/Finder backup on iOS to cover the most common WhatsApp migration case without requiring a compromised device.
- **`--verify-only` mode** — accept an existing repacked backup path and run the integrity checks (PRAGMA, plist, Manifest) without performing a restore. Useful for professionals validating a backup artifact independently.
- **Schema versioning layer** — detect the iOS version of the destination device and select the correct DB schema for injection rather than assuming a fixed layout. Prevents silent failures when Apple makes schema changes across major iOS releases.
- **Android SMS default-app prompting** — Android 10+ requires the default SMS app role to write messages. The tool should detect this, prompt the user to temporarily set PhoneTransfer as default, and guide them through reverting afterward.
- **Windows one-command install script** — a single PowerShell script that handles Python, pip dependencies, and the SQLCipher DLL for encrypted iOS backups. Currently requires manual steps.

---

## Later — design decisions needed

These are real improvements but require choices that go beyond code.

- **EV code signing for the exe build** — eliminates Windows SmartScreen warnings on the standalone build. Requires a certificate (~$300/year). Until then, source install is the recommended path.
- **Play Store distribution for the companion APK** — a Play Store version would remove the sideloading barrier for non-technical users, but Play Store policy restricts several of the APIs the companion relies on (SMS read, call log, contacts write). A limited-functionality Play Store version is possible; full functionality requires sideload.
- **iCloud-aware restore** — detect post-restore iCloud drift (contacts, calendar, notes re-syncing from iCloud and overwriting the transferred data) and warn the user with mitigation steps.
- **Health data without jailbreak** — HealthKit data is accessible via backup on iOS but requires the backup encryption key and is locked behind Apple's entitlement system on Android. No clean path exists without root today.
- **Compatibility matrix automation** — replace the static table in README.md with a generated matrix that reflects the actual state of each extractor/injector module, so it stays accurate as support changes.

---

## Not planned

- Cloud-based transfer (routing data through any server)
- Telemetry or usage analytics of any kind
- A subscription or account model
