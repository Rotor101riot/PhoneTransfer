"""
inject_mail_accounts_android.py

"Injects" mail account metadata to an Android device.

Mail accounts cannot be silently added to Android without user interaction
(AccountManager requires authentication flows).  Instead, we write a summary
JSON file to /sdcard/PhoneTransfer/mail_accounts.json so the user knows which
accounts to re-configure, and log the list to the transfer results.

Return value: number of accounts written to the summary file.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from core.adb_manager import ADBManager
from core.config_loader import get_config
from core.normalization_schema import MailAccount

logger = logging.getLogger(__name__)

_DEVICE_DIR = "/sdcard/PhoneTransfer"
_DEVICE_FILE = f"{_DEVICE_DIR}/mail_accounts.json"


def inject(
    serial: str,
    items: list[MailAccount],
    staging_dir: Path,
    is_rooted: bool = False,
) -> int:
    """
    Write a mail account summary to the Android device.

    Parameters
    ----------
    serial:      ADB device serial.
    items:       MailAccount records extracted from the source device.
    staging_dir: Local staging directory for temp files.
    is_rooted:   Unused — accounts are written as a JSON guide file.

    Returns
    -------
    int: Number of accounts written.
    """
    if not items:
        logger.info("inject_mail_accounts_android: no accounts to write.")
        return 0

    try:
        cfg = get_config()
        adb = ADBManager(cfg)
    except Exception as exc:
        logger.error("inject_mail_accounts_android: ADB init failed: %s", exc)
        return 0

    # Ensure directory exists
    adb.shell(serial, f"mkdir -p {_DEVICE_DIR}", timeout=10)

    # Build the summary
    summary = []
    for acct in items:
        entry: dict = {
            "email": acct.email,
            "account_type": acct.account_type,
            "display_name": acct.display_name,
        }
        if acct.server_host:
            entry["server_host"] = acct.server_host
        if acct.server_port:
            entry["server_port"] = acct.server_port
        summary.append(entry)

    # Write to a local temp file, then push
    tmp = Path(tempfile.mktemp(suffix=".json", dir=str(staging_dir)))
    try:
        tmp.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        ok = adb.push(serial, tmp, _DEVICE_FILE, timeout=30)
        if ok:
            logger.info(
                "inject_mail_accounts_android: wrote %d account(s) to %s",
                len(summary), _DEVICE_FILE,
            )
            return len(summary)
        else:
            logger.warning("inject_mail_accounts_android: push failed")
            return 0
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
