CATEGORY = "media"

def validate(items: list) -> list[str]:
    """
    Validate a list of MediaFile objects.
    Checks:
    - filename non-empty
    - local_path exists on disk (warn if not)
    - mime_type non-empty and contains '/'
    - file size > 0 (warn if 0 bytes or unreadable)
    """
    issues = []
    for i, m in enumerate(items):
        prefix = f"MediaFile[{i}] '{m.filename}'"
        if not m.filename:
            issues.append(f"{prefix}: empty filename")
        if not m.mime_type:
            issues.append(f"{prefix}: empty mime_type")
        elif "/" not in m.mime_type:
            issues.append(f"{prefix}: malformed mime_type '{m.mime_type}'")
        if m.local_path is None:
            issues.append(f"{prefix}: local_path is None")
        elif not m.local_path.exists():
            issues.append(f"{prefix}: local_path does not exist: {m.local_path}")
        elif m.local_path.stat().st_size == 0:
            issues.append(f"{prefix}: file is 0 bytes: {m.local_path}")
    return issues
