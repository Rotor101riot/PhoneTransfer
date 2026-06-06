"""
scripts/copy_apk.py
===================
Developer utility — run this after every Android build to bundle the freshly
compiled companion APK into the Python app's assets/ directory.

Usage
-----
    # Default: debug build (com.phonetransfer.companion.debug)
    python scripts/copy_apk.py

    # Release build (com.phonetransfer.companion)
    python scripts/copy_apk.py --release

    # Override Android project location
    python scripts/copy_apk.py --android-dir "D:/MyProjects/PhoneTransferCompanion"

What it does
------------
1. Locates the built APK in the Android project's build/outputs directory.
2. Copies it to  assets/companion.apk  (creates assets/ if missing).
3. Reads versionCode from  app/build.gradle.kts  (default: 1 if unreadable).
4. Writes  assets/companion_meta.json  with package, version_code, and variant
   so companion_installer.py can check against the installed app on the device.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

# Ensure Unicode characters in print() work regardless of the terminal's
# default encoding (cp1252 on Windows PowerShell would otherwise crash).
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── Paths ─────────────────────────────────────────────────────────────────────

_SCRIPT_DIR    = Path(__file__).parent
_REPO_ROOT     = _SCRIPT_DIR.parent          # PhoneTransfer/
_ASSETS        = _REPO_ROOT / "assets"

# Default Android project location (sibling directory structure assumed).
# Override with --android-dir if your checkout lives elsewhere.
_DEFAULT_ANDROID = (
    Path.home() / "Documents" / "PhoneTransferCompanion"
)

_PACKAGE_BASE = "com.phonetransfer.companion"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_version_code(gradle_file: Path) -> int:
    """Parse versionCode from app/build.gradle.kts. Returns 1 on failure."""
    if not gradle_file.exists():
        print(f"  [warn] build.gradle.kts not found at {gradle_file}")
        return 1
    text = gradle_file.read_text(encoding="utf-8")
    m = re.search(r"versionCode\s*=\s*(\d+)", text)
    if m:
        return int(m.group(1))
    print("  [warn] Could not parse versionCode from build.gradle.kts — using 1")
    return 1


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bundle the companion APK into PhoneTransfer/assets/."
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="Bundle the release build instead of debug (default: debug)",
    )
    parser.add_argument(
        "--android-dir",
        metavar="PATH",
        default=str(_DEFAULT_ANDROID),
        help=f"Path to the PhoneTransferCompanion project root "
             f"(default: {_DEFAULT_ANDROID})",
    )
    args = parser.parse_args()

    android_root = Path(args.android_dir)

    if args.release:
        apk_src  = android_root / "app/build/outputs/apk/release/app-release.apk"
        package  = _PACKAGE_BASE
        variant  = "release"
    else:
        apk_src  = android_root / "app/build/outputs/apk/debug/app-debug.apk"
        package  = f"{_PACKAGE_BASE}.debug"
        variant  = "debug"

    # ── Validate source APK ───────────────────────────────────────────────────
    if not apk_src.exists():
        print(f"ERROR: APK not found: {apk_src}")
        print()
        print("Build steps:")
        print("  1. Open PhoneTransferCompanion in Android Studio")
        if args.release:
            print("  2. Build → Generate Signed Bundle / APK → APK → Release")
        else:
            print("  2. Build → Build Bundle(s) / APK(s) → Build APK(s)")
        print("  3. Re-run this script")
        return 1

    # ── Copy APK ──────────────────────────────────────────────────────────────
    _ASSETS.mkdir(parents=True, exist_ok=True)
    dest = _ASSETS / "companion.apk"
    shutil.copy2(apk_src, dest)
    size_mb = dest.stat().st_size / 1_048_576
    print(f"✓  {apk_src.name}  →  {dest}  ({size_mb:.1f} MB)")

    # ── Read versionCode ──────────────────────────────────────────────────────
    gradle = android_root / "app/build.gradle.kts"
    version_code = _read_version_code(gradle)

    # ── Write metadata ────────────────────────────────────────────────────────
    meta = {
        "package":      package,
        "version_code": version_code,
        "variant":      variant,
    }
    meta_path = _ASSETS / "companion_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"✓  companion_meta.json  →  package={package!r}  version_code={version_code}  variant={variant!r}")
    print()
    print("Done. Rebuild / re-run PhoneTransfer to use the new APK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
