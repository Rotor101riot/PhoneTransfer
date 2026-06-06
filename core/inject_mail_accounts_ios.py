"""
inject_mail_accounts_ios.py

"Injects" mail account metadata to an iOS device.

Like Android, mail accounts cannot be silently provisioned on iOS without
MDM profiles or user interaction.  This module writes a summary JSON to
the device (via AFC) so the user knows which accounts to re-configure.

Return value: number of accounts written to the summary file.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from core.normalization_schema import MailAccount

logger = logging.getLogger(__name__)

_IOS_STAGING_DIR = "PhoneTransfer"
_IOS_FILENAME = "mail_accounts.json"


def inject(
    udid: str,
    items: list[MailAccount],
    staging_dir: Path,
    is_jailbroken: bool = False,
) -> int:
    """
    Write a mail account summary to the iOS device via AFC.

    Parameters
    ----------
    udid:            iOS device UDID.
    items:           MailAccount records extracted from the source device.
    staging_dir:     Local staging directory for temp files.
    is_jailbroken:   Unused.

    Returns
    -------
    int: Number of accounts written.
    """
    if not items:
        logger.info("inject_mail_accounts_ios: no accounts to write.")
        return 0

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

    # Write locally first
    tmp = Path(tempfile.mktemp(suffix=".json", dir=str(staging_dir)))
    try:
        tmp.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

        # Push via AFC
        try:
            from core.afc_connector import AFCConnector
            afc = AFCConnector(udid)
            try:
                afc.makedirs(f"/var/mobile/Media/{_IOS_STAGING_DIR}")
            except Exception:
                pass
            remote_path = f"/var/mobile/Media/{_IOS_STAGING_DIR}/{_IOS_FILENAME}"
            afc.push(str(tmp), remote_path)
            afc.close()

            logger.info(
                "inject_mail_accounts_ios: wrote %d account(s) to %s",
                len(summary), remote_path,
            )
            return len(summary)
        except Exception as exc:
            logger.warning(
                "inject_mail_accounts_ios: AFC push failed: %s", exc
            )
            return 0
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
