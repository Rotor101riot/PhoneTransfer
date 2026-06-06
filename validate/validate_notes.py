CATEGORY = "notes"

def validate(items: list) -> list[str]:
    """
    Validate a list of Note objects.
    Checks:
    - title non-empty
    - body non-empty (warn if both title and body are effectively empty)
    - title not longer than 255 chars (warn)
    """
    issues = []
    for i, n in enumerate(items):
        prefix = f"Note[{i}]"
        if not n.title.strip():
            issues.append(f"{prefix}: empty title")
        if not n.body.strip():
            issues.append(f"{prefix}: empty body")
        if len(n.title) > 255:
            issues.append(f"{prefix}: title exceeds 255 chars ({len(n.title)})")
    return issues
