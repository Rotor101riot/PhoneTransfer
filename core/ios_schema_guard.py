"""
ios_schema_guard.py

Validates the Photos.sqlite CoreData schema before any write is attempted.

Apple changes the CoreData model (entity IDs, column layout) across iOS major
versions.  If we INSERT rows with the wrong Z_ENT values, the CoreData store
becomes silently corrupt — photos won't appear, or Photos.app may crash.

This module queries the live DB and compares its Z_PRIMARYKEY entity IDs
against our known-good constants before any INSERT is allowed.  If anything
doesn't match, the caller receives ok=False and should abort the injection
(files remain on DCIM and will be indexed by medialibraryd eventually, but
the DB is left untouched and in a consistent state).
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Expected CoreData entity IDs
# These are the Z_ENT values observed from live Photos.sqlite databases.
# Z_NAME is the CoreData entity class name as stored in Z_PRIMARYKEY.Z_NAME.
# ---------------------------------------------------------------------------

_EXPECTED: dict[str, int] = {
    "AdditionalAssetAttributes": 1,
    "Asset":                     3,
    "ExtendedAttributes":        28,
    "InternalResource":          51,
    "Moment":                    58,
}

# Tables that must exist for our INSERTs to work.
_REQUIRED_TABLES: list[str] = [
    "ZASSET",
    "ZADDITIONALASSETATTRIBUTES",
    "ZEXTENDEDATTRIBUTES",
    "ZINTERNALRESOURCE",
    "ZMOMENT",
    "Z_PRIMARYKEY",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_photos_schema(
    conn: sqlite3.Connection,
    ios_version: str | None = None,
) -> tuple[bool, list[str]]:
    """
    Validate Photos.sqlite schema compatibility before any write.

    Checks:
      1. All required tables exist in sqlite_master.
      2. Required CoreData entities are present in Z_PRIMARYKEY.
      3. Entity Z_ENT values match our hardcoded constants.

    Parameters
    ----------
    conn        : Open sqlite3 connection to the local Photos.sqlite copy.
    ios_version : iOS version string for log context (e.g. "17.2.1"). Optional.

    Returns
    -------
    (ok, issues)
        ok     — True if schema is compatible and injection can proceed.
        issues — Human-readable list of problems found (empty when ok=True).
    """
    tag = f" (iOS {ios_version})" if ios_version else ""
    issues: list[str] = []

    # -- 1. Check required tables exist ------------------------------------
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing = {row[0] for row in cur.fetchall()}
    except Exception as exc:
        issues.append(f"Cannot query sqlite_master: {exc}")
        logger.error("schema_guard%s: %s", tag, issues[-1])
        return False, issues

    for table in _REQUIRED_TABLES:
        if table not in existing:
            issues.append(f"Required table missing: {table}")

    if issues:
        logger.error(
            "schema_guard%s: missing tables — schema is incompatible. "
            "Skipping Photos.sqlite injection to prevent corruption. "
            "Missing: %s",
            tag,
            ", ".join(t for t in _REQUIRED_TABLES if t not in existing),
        )
        return False, issues

    # -- 2. Read actual entity IDs from Z_PRIMARYKEY -----------------------
    try:
        placeholders = ",".join("?" * len(_EXPECTED))
        cur = conn.execute(
            f"SELECT Z_ENT, Z_NAME FROM Z_PRIMARYKEY WHERE Z_NAME IN ({placeholders})",
            list(_EXPECTED.keys()),
        )
        actual: dict[str, int] = {row[1]: row[0] for row in cur.fetchall()}
    except Exception as exc:
        issues.append(f"Cannot query Z_PRIMARYKEY: {exc}")
        logger.error("schema_guard%s: %s", tag, issues[-1])
        return False, issues

    # -- 3. Compare expected vs actual entity IDs --------------------------
    for name, expected_ent in _EXPECTED.items():
        if name not in actual:
            issues.append(
                f"CoreData entity '{name}' missing from Z_PRIMARYKEY "
                f"(expected Z_ENT={expected_ent})"
            )
        elif actual[name] != expected_ent:
            issues.append(
                f"CoreData entity '{name}': expected Z_ENT={expected_ent}, "
                f"got Z_ENT={actual[name]} — schema version mismatch"
            )

    if issues:
        logger.error(
            "schema_guard%s: entity ID mismatch detected. "
            "Skipping Photos.sqlite injection to prevent silent data corruption. "
            "Issues:\n  %s\n"
            "If this is a new iOS version, the schema constants in "
            "photos_sqlite_injector.py need to be updated for this device.",
            tag,
            "\n  ".join(issues),
        )
        logger.info(
            "schema_guard%s: photo files are already on the device in DCIM. "
            "They will appear in the Photos app after iOS media library re-scan "
            "(typically at next device lock/charge cycle).",
            tag,
        )
        return False, issues

    logger.debug(
        "schema_guard%s: schema validated — all %d entity IDs match.",
        tag,
        len(_EXPECTED),
    )
    return True, []


def log_actual_schema(conn: sqlite3.Connection) -> None:
    """
    Diagnostic helper: log the full Z_PRIMARYKEY contents at DEBUG level.
    Call this when schema validation fails to aid in updating the constants.
    """
    try:
        cur = conn.execute(
            "SELECT Z_ENT, Z_NAME, Z_MAX FROM Z_PRIMARYKEY ORDER BY Z_ENT"
        )
        rows = cur.fetchall()
        logger.debug(
            "schema_guard: full Z_PRIMARYKEY dump (%d entities):", len(rows)
        )
        for ent, name, zmax in rows:
            marker = " <-- MISMATCH" if (
                name in _EXPECTED and _EXPECTED[name] != ent
            ) else ""
            logger.debug(
                "  Z_ENT=%-4d  Z_NAME=%-40s  Z_MAX=%d%s",
                ent, name, zmax, marker,
            )
    except Exception as exc:
        logger.debug("schema_guard: could not dump Z_PRIMARYKEY: %s", exc)
