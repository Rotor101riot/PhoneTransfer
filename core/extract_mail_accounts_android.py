"""
extract_mail_accounts_android.py

Extracts configured email account metadata from an Android device.

Primary path: companion APK's ``extract_mail_accounts`` command via TCP socket.
Fallback:     ``adb shell dumpsys account`` parsing (less reliable but works
              without the companion installed).

Only account type and email address are extracted — no passwords or auth tokens.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from core.adb_manager import ADBManager
from core.config_loader import get_config
from core.normalization_schema import MailAccount

logger = logging.getLogger(__name__)

# AccountManager types that correspond to email accounts.
_EMAIL_ACCOUNT_TYPES = {
    "com.google",
    "com.google.android.gm.legacyimap",
    "com.microsoft.exchange",
    "com.microsoft.office.outlook",
    "com.android.exchange",
    "com.samsung.android.email.provider",
    "com.yahoo.mobile.client.android.im",
    "org.mozilla.thunderbird",
}

_EMAIL_KEYWORDS = ("mail", "imap", "pop3", "exchange", "email", "smtp")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract(
    serial: str,
    staging_dir: Path,
    is_rooted: bool = False,
) -> list[MailAccount]:
    """
    Extract email account metadata from the Android device.

    Parameters
    ----------
    serial:      ADB device serial string.
    staging_dir: Root staging directory (unused — no files written).
    is_rooted:   Unused for mail accounts.

    Returns
    -------
    list[MailAccount]
    """
    # Try companion first
    accounts = _extract_via_companion(serial)
    if accounts is not None:
        return accounts

    # Fallback to dumpsys
    return _extract_via_dumpsys(serial)


def _extract_via_companion(serial: str) -> list[MailAccount] | None:
    """Use the companion APK's socket handler."""
    try:
        from core.companion_app_protocol import CompanionClient, setup_adb_forward
        cfg = get_config()
        adb = ADBManager(cfg)
        setup_adb_forward(adb, serial)

        with CompanionClient() as client:
            if not client.ping():
                return None
            resp = client.extract("mail_accounts")
            if resp is None or resp.get("status") != "ok":
                return None

            data = resp.get("data", [])
            accounts: list[MailAccount] = []
            for entry in data:
                accounts.append(MailAccount(
                    email=entry.get("email", ""),
                    account_type=entry.get("account_type", ""),
                    display_name=entry.get("display_name", ""),
                ))
            logger.info(
                "extract_mail_accounts_android: companion returned %d account(s)",
                len(accounts),
            )
            return accounts
    except Exception as exc:
        logger.debug(
            "extract_mail_accounts_android: companion path failed: %s", exc
        )
        return None


def _extract_via_dumpsys(serial: str) -> list[MailAccount]:
    """Parse ``adb shell dumpsys account`` output as a fallback."""
    try:
        cfg = get_config()
        adb = ADBManager(cfg)
        stdout, _, rc = adb.shell(serial, "dumpsys account", timeout=15)
        if rc != 0 or not stdout:
            return []

        accounts: list[MailAccount] = []
        seen: set[str] = set()

        # Pattern: Account {name=user@example.com, type=com.google}
        pattern = re.compile(
            r"Account\s*\{name=([^,]+),\s*type=([^}]+)\}"
        )
        for match in pattern.finditer(stdout):
            name = match.group(1).strip()
            acct_type = match.group(2).strip()

            # Filter to email-related accounts
            type_lower = acct_type.lower()
            is_email = (
                acct_type in _EMAIL_ACCOUNT_TYPES
                or any(kw in type_lower for kw in _EMAIL_KEYWORDS)
            )
            if not is_email:
                continue

            key = f"{name}|{acct_type}"
            if key in seen:
                continue
            seen.add(key)

            accounts.append(MailAccount(
                email=name,
                account_type=acct_type,
                display_name=name,
            ))

        logger.info(
            "extract_mail_accounts_android: dumpsys found %d email account(s)",
            len(accounts),
        )
        return accounts

    except Exception as exc:
        logger.warning(
            "extract_mail_accounts_android: dumpsys fallback failed: %s", exc
        )
        return []
