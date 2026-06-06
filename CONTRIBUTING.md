# Contributing to PhoneTransfer

## Repo layout

This is a monorepo containing two components:

| Directory | What it is |
|-----------|-----------|
| `core/`, `convert/`, `ui/`, `validate/` | Python desktop app |
| `companion/` | Android companion APK (Kotlin/Gradle) |
| `assets/companion.apk` | Pre-built companion — sideloaded automatically by the desktop app |
| `bin/` | Bundled binaries (ADB, libimobiledevice, drivers) |
| `reference/` | Device lookup tables and schema definitions |

The companion APK is a **tightly coupled component**, not a standalone project.
If you're only modifying the Python side you can ignore `companion/` entirely.

### Working on the companion

Open `companion/` as a project in Android Studio (File → Open → select the
`companion/` folder, not the repo root). Gradle will sync automatically.

To update the APK that ships with the desktop app after making companion changes:

```powershell
# From the repo root
.\companion\build_release.ps1    # produces companion/app/build/outputs/apk/release/
copy companion\app\build\outputs\apk\release\app-release.apk assets\companion.apk
```

---

## Reporting bugs

Open a GitHub issue with:
- OS and Python version
- Source and destination device models + iOS/Android versions
- The categories that failed
- The relevant section of `tmp/logs/phonetransfer.log` (redact phone numbers if sharing publicly)

## Submitting a pull request

1. Fork the repo and create a branch from `master`.
2. Keep changes focused — one concern per PR.
3. If touching an extractor, injector, or the backup-mod pipeline, test against
   a real device pair or the dry-run pipeline (`G:\test\dry_run_pipeline.py`
   pattern — see `core/ios_backup_verify.py`).
4. PRs may be declined at maintainer discretion, for any reason.

## License

By submitting a pull request you agree that your contribution is licensed under
the GNU General Public License v2, the same license that covers this project.

You retain copyright on your own code. The maintainer retains copyright on all
original project code.

## Forks

GPL v2 grants anyone the right to fork this project and distribute their own
modified version, provided the GPL v2 terms are preserved. You do not need
permission to fork.
