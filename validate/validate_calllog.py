CATEGORY = "calls"

def validate(items: list) -> list[str]:
    """
    Validate a list of CallRecord objects.
    Checks:
    - number non-empty
    - timestamp not None
    - duration_seconds >= 0
    - call_type in {"incoming", "outgoing", "missed"}
    """
    issues = []
    valid_types = {"incoming", "outgoing", "missed"}
    for i, c in enumerate(items):
        prefix = f"CallRecord[{i}]"
        if not c.number:
            issues.append(f"{prefix}: empty phone number")
        if c.timestamp is None:
            issues.append(f"{prefix}: missing timestamp")
        if c.duration_seconds < 0:
            issues.append(f"{prefix}: negative duration ({c.duration_seconds}s)")
        if c.call_type not in valid_types:
            issues.append(f"{prefix}: unknown call_type '{c.call_type}'")
    return issues
