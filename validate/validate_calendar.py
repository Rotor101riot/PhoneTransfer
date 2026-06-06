CATEGORY = "calendar"

def validate(items: list) -> list[str]:
    """
    Validate a list of CalendarEvent objects.
    Checks:
    - title non-empty
    - start and end not None
    - end >= start (warn if end before start, unless all_day single-day)
    - recurrence_rule syntactically starts with "FREQ=" if set
    """
    issues = []
    for i, e in enumerate(items):
        prefix = f"CalendarEvent[{i}]"
        if not e.title:
            issues.append(f"{prefix}: empty title")
        if e.start is None:
            issues.append(f"{prefix}: missing start time")
        if e.end is None:
            issues.append(f"{prefix}: missing end time")
        if e.start and e.end and e.end < e.start:
            issues.append(f"{prefix}: end ({e.end}) is before start ({e.start})")
        if e.recurrence_rule and not e.recurrence_rule.startswith("FREQ="):
            issues.append(f"{prefix}: recurrence_rule does not start with FREQ=: '{e.recurrence_rule}'")
    return issues
