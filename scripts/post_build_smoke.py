"""
scripts/post_build_smoke.py — post-build verification for the PyInstaller exe.

Run after `pyinstaller PhoneTransfer.spec` to verify the produced binary:
  1. Checks dist/PhoneTransfer/PhoneTransfer.exe exists.
  2. Launches it with --smoke-test and verifies exit code 0.
  3. Checks that key data files were bundled (resources, ctk assets).

Usage:
    py scripts/post_build_smoke.py
    py scripts/post_build_smoke.py --dist-dir path/to/dist/PhoneTransfer

Exit code 0 = PASS, 1 = FAIL.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_GREEN = "\033[32m"
_RED = "\033[31m"
_RESET = "\033[0m"

_OK = f"{_GREEN}OK{_RESET}"
_FAIL = f"{_RED}FAIL{_RESET}"


def _check(label: str, ok: bool, detail: str = "") -> bool:
    status = _OK if ok else _FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")
    return ok


def main() -> None:
    p = argparse.ArgumentParser(description="Post-build smoke test for PhoneTransfer.exe")
    p.add_argument(
        "--dist-dir",
        default=None,
        help="Path to the dist/PhoneTransfer/ folder (default: auto-detect from project root)",
    )
    args = p.parse_args()

    project_root = Path(__file__).resolve().parent.parent

    if args.dist_dir:
        dist = Path(args.dist_dir).resolve()
    else:
        dist = project_root / "dist" / "PhoneTransfer"

    exe = dist / "PhoneTransfer.exe"

    print("\nPhoneTransfer post-build smoke test")
    print(f"  dist dir : {dist}")
    print(f"  exe      : {exe}\n")

    failures: list[str] = []

    # ── 1. Exe exists ─────────────────────────────────────────────────────────
    if not _check("exe exists", exe.is_file()):
        failures.append("exe missing — did pyinstaller finish?")

    # ── 2. --smoke-test import check ─────────────────────────────────────────
    # The exe is windowed (no console), so it writes results to a temp JSON
    # file (%TEMP%/phonetransfer_smoke.json) instead of stdout/stderr.
    if exe.is_file():
        import json as _json
        import tempfile as _tempfile

        smoke_json = Path(_tempfile.gettempdir()) / "phonetransfer_smoke.json"
        if smoke_json.exists():
            smoke_json.unlink()  # clear stale result from a previous run

        try:
            result = subprocess.run(
                [str(exe), "--smoke-test"],
                timeout=90,
            )
        except subprocess.TimeoutExpired:
            _check("--smoke-test imports", False, "timed out after 90s")
            failures.append("smoke-test timed out")
            result = None
        except Exception as exc:
            _check("--smoke-test imports", False, str(exc))
            failures.append(f"could not launch exe: {exc}")
            result = None

        if result is not None:
            if smoke_json.exists():
                try:
                    import_results = _json.loads(smoke_json.read_text(encoding="utf-8"))
                    failed_mods = [r for r in import_results if not r.get("ok")]
                    ok = len(failed_mods) == 0
                    detail = f"{len(import_results) - len(failed_mods)}/{len(import_results)} modules OK"
                    _check("--smoke-test imports", ok, detail)
                    if failed_mods:
                        failures.append("import failures:")
                        for r in failed_mods:
                            print(f"      FAIL  {r['module']}: {r.get('error', '')}")
                except Exception as exc:
                    _check("--smoke-test imports", False, f"could not parse results: {exc}")
                    failures.append("smoke result parse error")
            else:
                _check(
                    "--smoke-test imports",
                    False,
                    f"result file not written ({smoke_json})",
                )
                failures.append("smoke-test did not produce result file")

    # ── 3. Key data files bundled ─────────────────────────────────────────────
    _bundled_checks = [
        (
            "pymobiledevice3 resources",
            dist / "pymobiledevice3" / "resources" / "dsc_uuid_map.json",
        ),
        (
            "pymobiledevice3 webinspector JS",
            dist / "pymobiledevice3" / "resources" / "webinspector" / "focus.js",
        ),
        (
            "customtkinter theme assets",
            dist / "customtkinter" / "assets" / "themes" / "blue.json",
        ),
        (
            "reference dir present",
            dist / "reference",
        ),
    ]
    for label, path in _bundled_checks:
        if not _check(label, path.exists(), str(path.relative_to(dist))):
            failures.append(f"missing bundled file: {path.relative_to(dist)}")

    # ── 4. No .pyc-only modules (debug sanity) ────────────────────────────────
    pyc_only: list[Path] = []
    internal = dist / "_internal"
    search_root = internal if internal.is_dir() else dist
    for pyc in search_root.rglob("*.pyc"):
        src = pyc.with_suffix(".py")
        if not src.exists():
            pyc_only.append(pyc)
    # Only flag if suspiciously many — a handful of stdlib .pyc-only files is normal
    _check(
        ".pyc-only source files",
        len(pyc_only) < 20,
        f"{len(pyc_only)} found (< 20 is normal)",
    )

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if failures:
        print(f"{_RED}FAIL{_RESET} — {len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print(f"{_GREEN}PASS{_RESET} — all checks passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
