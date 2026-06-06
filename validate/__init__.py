"""validate package — data validators for each transfer category."""

from __future__ import annotations
import importlib

# Map category name → module name
_VALIDATORS = {
    "contacts":  "validate_contacts",
    "sms":       "validate_sms",
    "calls":     "validate_calllog",
    "calendar":  "validate_calendar",
    "notes":     "validate_notes",
    "media":     "validate_media",
    "signal":    "validate_signal",
    "whatsapp":  "validate_whatsapp",
    "health":    "validate_health",
}

def validate_category(category: str, items: list) -> list[str]:
    """Run the validator for a category. Returns list of issue strings."""
    mod_name = _VALIDATORS.get(category)
    if not mod_name:
        return []
    try:
        mod = importlib.import_module(f"validate.{mod_name}")
        return mod.validate(items)
    except Exception as exc:
        return [f"Validator error for {category}: {exc}"]

def validate_all(manifest) -> dict[str, list[str]]:
    """
    Run all validators on a TransferManifest.
    Returns dict mapping category → list[str] issues.
    Only entries with issues are included.
    """
    results: dict[str, list[str]] = {}
    checks: dict[str, list] = {
        "contacts":  manifest.contacts,
        "sms":       manifest.messages,
        "calls":     manifest.calls,
        "calendar":  manifest.events,
        "notes":     manifest.notes,
        "media":     manifest.media,
    }
    for cat, items in checks.items():
        issues = validate_category(cat, items)
        if issues:
            results[cat] = issues
    return results
