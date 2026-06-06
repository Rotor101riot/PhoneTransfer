CATEGORY = "signal"

def validate(items: list) -> list[str]:
    """
    Validate a list of objects from convert.convert_signal (SignalMessage or dicts).
    Checks only basic fields since Signal data is often incomplete.
    Always prepends a note that Signal data may be incomplete due to encryption.
    """
    issues = []
    if items:
        issues.append(
            "Signal: transfer of Signal messages is limited — encryption keys are "
            "device-bound and the full message history may not be recoverable. "
            f"{len(items)} record(s) found."
        )
    for i, m in enumerate(items):
        prefix = f"SignalMessage[{i}]"
        body = getattr(m, "body", None) or (m.get("body") if isinstance(m, dict) else None)
        ts   = getattr(m, "timestamp", None) or (m.get("timestamp") if isinstance(m, dict) else None)
        if not body:
            issues.append(f"{prefix}: empty body")
        if ts is None:
            issues.append(f"{prefix}: missing timestamp")
    return issues
