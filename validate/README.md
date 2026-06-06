# validate/

Post-extraction data validators.  These modules are called by the pipeline
**at runtime** (not just during testing) to check the quality of extracted
records before injection begins.

---

## How it works

```
pipeline_manager.py
  └─ validate.validate_all(manifest)
       ├─ validate_contacts.validate(items)   → list[str] issues
       ├─ validate_sms.validate(items)        → list[str] issues
       ├─ validate_calllog.validate(items)    → list[str] issues
       └─ … (one module per category)
```

Each validator returns a list of human-readable issue strings.  An empty
list means the category passed validation.  Issues are logged as warnings
and shown in the transfer summary — they do not abort the transfer.

The `validate_all()` entry point (in `__init__.py`) accepts a
`TransferManifest` and runs all registered validators, returning a dict
of `category → [issues]` for only the categories that have problems.

---

## Difference from `tests/`

| Directory | Purpose | When it runs |
|-----------|---------|--------------|
| `validate/` | **Runtime data quality checks** — validates extracted records for completeness, encoding issues, malformed phone numbers, empty required fields, etc. | During every real transfer, before injection |
| `tests/` | **Developer correctness tests** — unit and integration tests for the Python modules themselves | During CI and local development |

`validate/` modules are **not** test files — `pytest` will collect them
but they have no test functions.  They are production code.

---

## Validators

| Module | Category | What it checks |
|--------|----------|----------------|
| `validate_contacts.py` | contacts | Required fields (first/last name or at least one phone), phone number format, duplicate detection |
| `validate_sms.py` | sms | Non-empty body or attachment, valid timestamp, sender/recipient present |
| `validate_calllog.py` | calls | Valid call type, non-negative duration, timestamp in reasonable range |
| `validate_calendar.py` | calendar | Start < end, timezone present, non-empty title |
| `validate_notes.py` | notes | Non-empty content, valid timestamp |
| `validate_media.py` | media | File exists at local_path, non-zero size, recognised MIME type |
| `validate_signal.py` | signal | Same checks as sms + attachment paths resolvable |
| `validate_whatsapp.py` | whatsapp | Same checks as sms + media attachment size sanity |
| `validate_health.py` | health | Numeric value in range, valid unit, timestamp present |

---

## Adding a validator

1. Create `validate/validate_<category>.py` with a `validate(items: list) -> list[str]` function.
2. Register it in `validate/__init__.py` by adding `"<category>": "validate_<category>"` to `_VALIDATORS`.
3. Add the category to the `checks` dict in `validate_all()` if it reads from `TransferManifest`.
