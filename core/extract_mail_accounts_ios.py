"""
extract_mail_accounts_ios.py

Extracts configured email account metadata from an iOS backup.

iOS stores mail account configuration in ``Accounts4.sqlite`` within the
``HomeDomain-Library/Accounts`` backup domain.  Each row in the ZACCOUNT
table has the account type, description, and credential reference — but
we only extract the metadata (email, type, display name), never passwords.

Falls back to the ``com.apple.mail`` preference plist if the Accounts DB
is unavailable.
"""

from __future__ import annotations

import logging
import plistlib
import sqlite3
from pathlib import Path

from core.normalization_schema import MailAccount

logger = logging.getLogger(__name__)

# Backup-relative domain and file paths for mail accounts
_ACCOUNTS_DB_DOMAIN = "HomeDomain"
_ACCOUNTS_DB_PATH = "Library/Accounts/Accounts4.sqlite"
_MAIL_PLIST_DOMAIN = "HomeDomain"
_MAIL_PLIST_PATH = "Library/Preferences/com.apple.mail.plist"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract(
    udid: str,
    staging_dir: Path,
    is_jailbroken: bool = False,
) -> list[MailAccount]:
    """
    Extract email account metadata from an iOS backup.

    Parameters
    ----------
    udid:           iOS device UDID.
    staging_dir:    Root staging directory for this transfer session.
    is_jailbroken:  Unused — accounts are read from backup, not live device.

    Returns
    -------
    list[MailAccount]
    """
    accounts = _extract_from_accounts_db(udid, staging_dir)
    if accounts:
        return accounts

    return _extract_from_mail_plist(udid, staging_dir)


def _extract_from_accounts_db(
    udid: str,
    staging_dir: Path,
) -> list[MailAccount]:
    """Read Accounts4.sqlite from the iOS backup."""
    try:
        from core.backup_parser_ios import BackupParser
        from core.config_loader import get_config

        parser = BackupParser(udid, get_config())
        db_path = parser.extract_file(
            _ACCOUNTS_DB_DOMAIN, _ACCOUNTS_DB_PATH, staging_dir
        )
        if db_path is None or not Path(db_path).exists():
            return []

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        accounts: list[MailAccount] = []

        # ZACCOUNT table: ZACCOUNTTYPEDESCRIPTION, ZUSERNAME, ZACCOUNTDESCRIPTION
        try:
            rows = conn.execute(
                "SELECT ZUSERNAME, ZACCOUNTTYPEDESCRIPTION, ZACCOUNTDESCRIPTION "
                "FROM ZACCOUNT "
                "WHERE ZUSERNAME IS NOT NULL AND ZUSERNAME != ''"
            ).fetchall()

            _mail_keywords = ("mail", "imap", "pop", "exchange", "gmail", "yahoo",
                              "outlook", "icloud", "aol", "smtp")

            for row in rows:
                username = row["ZUSERNAME"] or ""
                acct_type = row["ZACCOUNTTYPEDESCRIPTION"] or ""
                display = row["ZACCOUNTDESCRIPTION"] or username

                # Filter to email-related accounts
                combined = f"{acct_type} {username} {display}".lower()
                if not any(kw in combined for kw in _mail_keywords) and "@" not in username:
                    continue

                accounts.append(MailAccount(
                    email=username,
                    account_type=acct_type,
                    display_name=display,
                ))
        finally:
            conn.close()

        logger.info(
            "extract_mail_accounts_ios: Accounts4.sqlite yielded %d account(s)",
            len(accounts),
        )
        return accounts

    except Exception as exc:
        logger.debug(
            "extract_mail_accounts_ios: Accounts4.sqlite failed: %s", exc
        )
        return []


def _extract_from_mail_plist(
    udid: str,
    staging_dir: Path,
) -> list[MailAccount]:
    """Fallback: read com.apple.mail.plist from the backup."""
    try:
        from core.backup_parser_ios import BackupParser
        from core.config_loader import get_config

        parser = BackupParser(udid, get_config())
        plist_path = parser.extract_file(
            _MAIL_PLIST_DOMAIN, _MAIL_PLIST_PATH, staging_dir
        )
        if plist_path is None or not Path(plist_path).exists():
            return []

        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)

        accounts: list[MailAccount] = []

        # The plist may contain MailAccounts array
        mail_accounts = plist.get("MailAccounts", [])
        for acct in mail_accounts:
            email = acct.get("AccountName", "")
            if not email or email == "On My Mac":
                continue

            acct_type = acct.get("AccountType", "")
            display = acct.get("FullUserName", email)
            host = acct.get("Hostname", None)
            port = acct.get("PortNumber", None)

            accounts.append(MailAccount(
                email=email,
                account_type=acct_type,
                display_name=display,
                server_host=host,
                server_port=port,
            ))

        logger.info(
            "extract_mail_accounts_ios: mail plist yielded %d account(s)",
            len(accounts),
        )
        return accounts

    except Exception as exc:
        logger.debug(
            "extract_mail_accounts_ios: mail plist fallback failed: %s", exc
        )
        return []
