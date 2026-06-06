"""
main.py

Entry point for PhoneTransfer.

Usage:
    python main.py            # Launch GUI
    python main.py --version  # Print version and exit
"""

from __future__ import annotations

import sys

# Hard version gate.  The codebase uses PEP 604 union syntax (`int | None`)
# which is a syntax error before 3.10, so a friendlier message here beats a
# cryptic SyntaxError from the first imported module.
if sys.version_info < (3, 10):
    sys.stderr.write(
        f"PhoneTransfer requires Python 3.10 or newer "
        f"(found {sys.version.split()[0]}).\n"
        "Install a newer Python from https://www.python.org/downloads/ "
        "and re-run with that interpreter.\n"
    )
    sys.exit(1)

import argparse
import ctypes
import logging
import os
from pathlib import Path

__version__ = "1.0.0"

# ── Crash handler ────────────────────────────────────────────────────────────

def _install_crash_handler() -> None:
    """
    Install a sys.excepthook that writes a JSON crash report to tmp/crashes/
    before the process exits.  Never raises — failure is silently swallowed.
    """
    import json
    import platform
    import traceback

    crash_dir = Path(__file__).resolve().parent / "tmp" / "crashes"

    def _hook(exc_type, exc_value, exc_tb) -> None:
        try:
            crash_dir.mkdir(parents=True, exist_ok=True)
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            report = {
                "timestamp": ts,
                "app_version": __version__,
                "python": sys.version,
                "platform": platform.platform(),
                "exception_type": exc_type.__qualname__,
                "exception_message": str(exc_value),
                "traceback": traceback.format_exception(exc_type, exc_value, exc_tb),
            }
            out = crash_dir / f"crash_{ts}.json"
            out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            sys.stderr.write(
                f"\n[PhoneTransfer] Crash report saved to {out}\n"
                "Please attach it when reporting this bug.\n\n"
            )
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


# ── UAC elevation ────────────────────────────────────────────────────────────

def _is_admin() -> bool:
    """Return True if the current process has administrator privileges."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except AttributeError:
        return False  # Not on Windows


def _elevate() -> None:
    """Re-launch the current script with a UAC elevation prompt, then exit."""
    script = os.path.abspath(sys.argv[0])
    params = " ".join(f'"{a}"' for a in sys.argv[1:])

    # Use pythonw.exe's parent (python.exe) to keep the console visible
    python = sys.executable

    # ShellExecuteW returns >32 on success, <=32 on failure
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", python, f'"{script}" {params}'.strip(), None, 1,
    )
    if ret <= 32:
        print("[ERROR] UAC elevation was declined or failed.", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


# ── Logging setup ────────────────────────────────────────────────────────────

class _JsonFormatter(logging.Formatter):
    """One-JSON-object-per-line formatter for log-ingestion pipelines."""

    def format(self, record: logging.LogRecord) -> str:
        import json as _json
        return _json.dumps(
            {
                "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            },
            ensure_ascii=False,
        )


def _setup_logging() -> None:
    """Configure root logger: INFO to stderr + rotating file in tmp/."""
    import logging.handlers

    fmt = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, stream=sys.stderr)

    try:
        log_dir = Path(__file__).resolve().parent / "tmp" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_dir / "phonetransfer.log",
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,
            encoding="utf-8",
        )
        try:
            from core.settings_manager import get_settings
            _use_json = get_settings().log_format == "json"
        except Exception:
            _use_json = False
        fh.setFormatter(_JsonFormatter() if _use_json else logging.Formatter(fmt))
        try:
            from core.pii_filter import PiiRedactFilter
            fh.addFilter(PiiRedactFilter())
        except Exception:
            pass
        logging.getLogger().addHandler(fh)
    except OSError:
        pass  # Can't write log file — console only is fine


# ── CLI parsing ──────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="PhoneTransfer",
        description="Free & open-source phone-to-phone data transfer.",
    )
    p.add_argument(
        "--version", action="version",
        version=f"PhoneTransfer {__version__}",
    )
    p.add_argument(
        "--smoke-test", action="store_true",
        help="Import all critical modules and exit 0 (used by post-build verification).",
    )
    return p.parse_args()


def _run_smoke_test() -> None:
    """
    Import all critical runtime modules and write results to a temp JSON file.

    Called by scripts/post_build_smoke.py immediately after the pyinstaller
    build.  The windowed exe has no console, so stdout/stderr are not visible
    to the parent process.  Results are written to
    %TEMP%/phonetransfer_smoke.json and read back by the smoke script.

    Exits 0 on full success, 1 on any failure.
    """
    import importlib
    import json
    import os
    import tempfile

    _REQUIRED = [
        # Core pipeline
        "core.pipeline_manager",
        "core.companion_app_protocol",
        "core.ios_backup_repacker",
        "core.ios_backup_injector",
        "core.ios_backup_verify",
        "core.config_loader",
        "core.settings_manager",
        "core.session_manager",
        "core.device_detector",
        # Convert helpers
        "convert.convert_contacts",
        "convert.convert_sms",
        "convert.convert_calllog",
        "convert.convert_calendar",
        "convert.convert_audio",
        "convert.convert_notes",
        # Heavy third-party
        "pymobiledevice3",
        "pymobiledevice3.lockdown",
        "pymobiledevice3.services.afc",
        "pymobiledevice3.services.mobilebackup2",
        "iphone_backup_decrypt",
        "iOSbackup",
        "customtkinter",
        "Crypto.Cipher.AES",
        "cryptography.hazmat.primitives.ciphers",
        "vobject",
        "plistlib",
        "sqlite3",
    ]

    results: list[dict] = []
    for mod in _REQUIRED:
        try:
            importlib.import_module(mod)
            results.append({"module": mod, "ok": True})
        except Exception as exc:
            results.append({"module": mod, "ok": False, "error": str(exc)})

    out_path = os.path.join(tempfile.gettempdir(), "phonetransfer_smoke.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    failed = [r for r in results if not r["ok"]]
    sys.exit(1 if failed else 0)


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()

    if args.smoke_test:
        _run_smoke_test()  # exits internally

    # Request admin privileges before anything else — driver installation
    # and pnputil driver enumeration require elevation.
    if not _is_admin():
        print("  Requesting administrator privileges...")
        _elevate()

    _install_crash_handler()
    _setup_logging()

    logger = logging.getLogger(__name__)
    logger.info("PhoneTransfer %s starting up.", __version__)

    # ── Prerequisite checks ───────────────────────────────────────────────────
    # Run before anything else so missing packages / runtimes / drivers surface
    # immediately with clear messages rather than cryptic errors mid-transfer.
    # Startup is BLOCKED until all fixable prerequisites are resolved.
    try:
        from core.prerequisite_checker import PrereqChecker
        checker = PrereqChecker()

        print("\n  Checking prerequisites...")
        report = checker.check_all()

        if not report.all_ok:
            print("  Some prerequisites need attention — installing...\n")
            logger.info("Prerequisites need attention — running fixes...")
            report = checker.fix_all(report, prompt_fn=None)

        if report.needs_attention:
            print("\n  [!] The following items still need attention:")
            for item in report.needs_attention:
                print(f"      - {item}")
                logger.warning("[prereq] %s", item)
            print()
        else:
            print("  All prerequisites satisfied.\n")
            logger.info("All prerequisites satisfied.")
    except Exception as exc:
        logger.warning("Prerequisite checker error (non-fatal): %s", exc)
        print(f"\n  [!] Prerequisite check error: {exc}\n")

    # ── Config validation ─────────────────────────────────────────────────────
    # Validate config early so the user sees missing-binary errors immediately,
    # not mid-transfer.
    try:
        from core.config_loader import get_config
        cfg = get_config()
        logger.info("Config OK — project root: %s", cfg.project_root)
    except FileNotFoundError as exc:
        print(f"[ERROR] Configuration problem: {exc}", file=sys.stderr)
        print(
            "Make sure the bin/ directory contains adb/, ffmpeg/, and libimobiledevice/.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── Device database update + initial scan ────────────────────────────────
    # Run before the GUI so connected devices show proper names immediately.
    initial_devices = None
    try:
        print("  Updating device databases...")
        from reference.enrich_device_lookups import enrich_all
        from reference.device_names import refresh_caches
        enrich_all()
        refresh_caches()

        print("  Scanning for connected devices...")
        from core.device_detector import detect_all_devices
        initial_devices = detect_all_devices(cfg)
        n = len(initial_devices)
        if n:
            print(f"  Found {n} device(s).")
        else:
            print("  No devices connected (you can plug in later).")
    except Exception as exc:
        logger.warning("Pre-scan failed (non-fatal): %s", exc)

    # ── Per-monitor DPI awareness ─────────────────────────────────────────────
    # Must be set before any window is created.  Without this, customtkinter
    # renders blurry on 150%/200% Windows displays and hit-testing is wrong.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # Fallback: Windows 7 / Vista
        except (AttributeError, OSError):
            pass

    # ── Launch GUI ────────────────────────────────────────────────────────────
    try:
        from ui.main_window import MainWindow
        app = MainWindow(initial_devices=initial_devices)
        app.mainloop()
    except ImportError as exc:
        print(
            f"[ERROR] Could not import UI: {exc}\n"
            "Install dependencies with: pip install customtkinter",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
