CATEGORY = "whatsapp"

def validate(items: list) -> list[str]:
    """
    Validate WhatsApp message objects (WhatsAppMessage or dicts).
    Checks sender, body, timestamp.
    Prepends a note about WhatsApp encryption limitations.
    """
    issues = []
    if items:
        issues.append(
            "WhatsApp: only exported chat text (.txt) is supported for migration. "
            "Media attachments require manual transfer. "
            f"{len(items)} message(s) found."
        )
    for i, m in enumerate(items):
        prefix = f"WhatsAppMessage[{i}]"
        sender = getattr(m, "sender", None) or (m.get("sender") if isinstance(m, dict) else None)
        body   = getattr(m, "body", None)   or (m.get("body")   if isinstance(m, dict) else None)
        ts     = getattr(m, "timestamp", None) or (m.get("timestamp") if isinstance(m, dict) else None)
        if not sender:
            issues.append(f"{prefix}: missing sender")
        if not body:
            issues.append(f"{prefix}: empty body")
        if ts is None:
            issues.append(f"{prefix}: missing timestamp")
    return issues
