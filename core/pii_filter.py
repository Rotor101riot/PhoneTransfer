"""
core/pii_filter.py

PII redaction utilities for log output.

Exposes:
  - ``redact_phone(s)``  — replace phone numbers with [PHONE]
  - ``redact_email(s)``  — replace email addresses with [EMAIL]
  - ``redact(s)``        — apply all redaction passes
  - ``PiiRedactFilter``  — logging.Filter that redacts log message strings

Attach PiiRedactFilter to any log handler that writes to disk or is shared
with external systems.  Console handlers during development can skip it.

Usage
-----
    from core.pii_filter import PiiRedactFilter
    file_handler.addFilter(PiiRedactFilter())
"""

from __future__ import annotations

import logging
import re

# E.164 / NANP / local: optional leading +, 10-15 consecutive digits possibly
# separated by spaces, dashes, dots, or parentheses.  We require at least
# 7 contiguous digits to avoid false-positives on short numeric IDs.
_RE_PHONE = re.compile(
    r'(?<!\d)'           # not preceded by a digit
    r'(\+?[\d\s\-\.\(\)]{0,4}[\d]{3}[\s\-\.\(\)]{0,2}[\d]{3}[\s\-\.]{0,2}[\d]{4,6})'
    r'(?!\d)',           # not followed by a digit
)

_RE_EMAIL = re.compile(
    r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'
)


def redact_phone(s: str) -> str:
    return _RE_PHONE.sub("[PHONE]", s)


def redact_email(s: str) -> str:
    return _RE_EMAIL.sub("[EMAIL]", s)


def redact(s: str) -> str:
    """Apply phone and email redaction to *s*."""
    return redact_email(redact_phone(s))


class PiiRedactFilter(logging.Filter):
    """
    Logging filter that redacts phone numbers and email addresses from the
    formatted log message before the record reaches its handler.

    Attach to file handlers or any handler whose output may be shared:

        handler.addFilter(PiiRedactFilter())
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(str(record.msg))
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: redact(str(v)) if isinstance(v, str) else v
                        for k, v in record.args.items()
                    }
                else:
                    record.args = tuple(
                        redact(str(a)) if isinstance(a, str) else a
                        for a in record.args
                    )
        except Exception:
            pass  # never block a log record
        return True
