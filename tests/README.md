# tests/

Unit and integration tests for PhoneTransfer.

---

## Current state

| File | What it covers | Needs device? |
|------|---------------|---------------|
| `test_tier1.py` | Tier 1 modules — normalization schema, content dedup, PII filter, settings manager, vCard serialisation, iMessage→MMS conversion, call type mapping, quirk detector | No |

`test_tier1.py` uses a minimal self-contained test harness (no pytest required, though pytest discovers and runs it correctly). Run it directly:

```
python tests/test_tier1.py
```

Or via pytest (recommended — picks up CI timeouts and better output):

```
pytest tests/ -v --timeout=30
```

---

## Tier naming

The `tier1` label reflects a planned validation gradient:

| Tier | Scope | Status |
|------|-------|--------|
| **Tier 1** | Pure-Python logic — no device, no FFmpeg, no pillow-heif. Schema validation, data transformations, dedup logic, PII filtering, quirk matching. | **Exists** (`test_tier1.py`) |
| **Tier 2** | Extractor/injector integration — requires a real or emulated device (Android emulator via ADB, or an iOS backup fixture on disk). | Planned — not yet written |
| **Tier 3** | End-to-end pipeline — full transfer between two connected devices. Only runs in the lab environment. | Planned — not yet written |

Tier 2 and Tier 3 tests belong in `tests/integration/` (excluded from the CI `pytest` run via `--ignore=tests/integration`).

---

## Adding a test

Tier 1 tests do not require any installed packages beyond what is in
`requirements.txt`. If your test needs a device, an emulator, or a binary
(FFmpeg, ADB), put it in `tests/integration/` instead.

New tier 1 tests can use either the existing lightweight `_run()` harness
in `test_tier1.py` or standard `pytest` conventions — pytest collects both.
