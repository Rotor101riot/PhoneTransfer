CATEGORY = "contacts"

def validate(items: list) -> list[str]:
    """
    Validate a list of Contact objects.
    Checks:
    - At least one phone or email per contact (warn if neither)
    - Phone numbers non-empty after stripping whitespace
    - Email addresses contain '@'
    - Warn if both first_name and last_name are None/empty
    Returns list of warning strings (index-prefixed for traceability).
    """
    import re
    issues = []
    for i, c in enumerate(items):
        prefix = f"Contact[{i}]"
        has_phone = any(p.strip() for p in c.phones)
        has_email = any(e.strip() for e in c.emails)
        if not has_phone and not has_email:
            issues.append(f"{prefix}: no phone or email — contact may be empty")
        for p in c.phones:
            if not re.sub(r"[\s\-\(\)\+]", "", p):
                issues.append(f"{prefix}: blank phone entry")
        for e in c.emails:
            if "@" not in e:
                issues.append(f"{prefix}: invalid email '{e}'")
        if not c.first_name and not c.last_name:
            issues.append(f"{prefix}: no name (first_name and last_name both empty)")
    return issues
