CATEGORY = "sms"

def validate(items: list) -> list[str]:
    """
    Validate a list of Message objects.
    Checks:
    - sender and recipient non-empty
    - timestamp is a datetime object (not None)
    - body non-empty for SMS (warn if empty body and no attachments)
    - service must be in {"sms", "mms", "imessage"}
    """
    issues = []
    valid_services = {"sms", "mms", "imessage", "rcs"}
    for i, m in enumerate(items):
        prefix = f"Message[{i}]"
        if not m.sender:
            issues.append(f"{prefix}: empty sender")
        if not m.recipient:
            issues.append(f"{prefix}: empty recipient")
        if m.timestamp is None:
            issues.append(f"{prefix}: missing timestamp")
        if not m.body and not m.attachments:
            issues.append(f"{prefix}: empty body and no attachments")
        if m.service not in valid_services:
            issues.append(f"{prefix}: unknown service '{m.service}'")
    return issues
