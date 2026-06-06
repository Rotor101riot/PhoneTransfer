CATEGORY = "health"

def validate(items: list) -> list[str]:
    """
    Validate HealthSample objects (or dicts).
    Checks type, value, start/end timestamps.
    Prepends note about health migration limitations.
    """
    issues = []
    if items:
        issues.append(
            "Health: full iOS → Android health migration requires HealthKit entitlement "
            "on iOS and Health Connect on Android. Only backup-accessible records are shown. "
            f"{len(items)} sample(s) found."
        )
    for i, s in enumerate(items):
        prefix = f"HealthSample[{i}]"
        htype = getattr(s, "type", None) or (s.get("type") if isinstance(s, dict) else None)
        value = getattr(s, "value", None)
        start = getattr(s, "start", None)
        if not htype:
            issues.append(f"{prefix}: missing type")
        if value is None:
            issues.append(f"{prefix}: missing value")
        if start is None:
            issues.append(f"{prefix}: missing start timestamp")
    return issues
