from __future__ import annotations

import logging
import re
import sqlite3
import time
import uuid
from pathlib import Path

from core.ios_backup_injector import IOSBackupInjector, get_current_injector
from core.normalization_schema import Bookmark

logger = logging.getLogger(__name__)

_AFC_DEST_DIR = "/var/mobile/Media/PhoneTransfer"
_AFC_DEST_FILE = f"{_AFC_DEST_DIR}/bookmarks.html"

# Bookmarks.db constants (see G:/test/modify_safari.py).
_BOOKMARKS_DOMAIN = "HomeDomain"
_BOOKMARKS_RELPATH = "Library/Safari/Bookmarks.db"
_APPLE_EPOCH_OFFSET = 978307200
_BOOKMARKSBAR_ID = 1  # special_id=1 = BookmarksBar folder
_TYPE_BOOKMARK = 0
_TYPE_FOLDER = 1
_TITLE_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _datetime_to_add_date(dt) -> str:
    """Return a Unix timestamp string suitable for ADD_DATE attribute."""
    try:
        return str(int(dt.timestamp()))
    except Exception:
        return "0"


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_netscape_html(items: list[Bookmark]) -> str:
    """Build a Netscape Bookmark HTML string from Bookmark objects.

    Bookmarks with the same folder are grouped under a DL/H3 heading.
    Bookmarks with no folder go directly under the top-level "Imported"
    folder.
    """
    # Group by folder
    from collections import defaultdict

    grouped: dict[str | None, list[Bookmark]] = defaultdict(list)
    for bm in items:
        grouped[bm.folder].append(bm)

    lines = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        "<!-- This is an automatically generated file. -->",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        "<TITLE>Bookmarks</TITLE>",
        "<H1>Bookmarks</H1>",
        "<DL><p>",
        "    <DT><H3>Imported from PhoneTransfer</H3>",
        "    <DL><p>",
    ]

    # Emit bookmarks with no folder first
    for bm in grouped.get(None, []):
        add_date = _datetime_to_add_date(bm.added) if bm.added else "0"
        title = _escape_html(bm.title or bm.url)
        url = _escape_html(bm.url)
        lines.append(
            f'        <DT><A HREF="{url}" ADD_DATE="{add_date}">{title}</A>'
        )

    # Emit per-folder groups
    for folder, bms in grouped.items():
        if folder is None:
            continue
        folder_escaped = _escape_html(folder)
        lines.append(f"        <DT><H3>{folder_escaped}</H3>")
        lines.append("        <DL><p>")
        for bm in bms:
            add_date = _datetime_to_add_date(bm.added) if bm.added else "0"
            title = _escape_html(bm.title or bm.url)
            url = _escape_html(bm.url)
            lines.append(
                f'            <DT><A HREF="{url}" ADD_DATE="{add_date}">{title}</A>'
            )
        lines.append("        </DL><p>")

    lines += [
        "    </DL><p>",
        "</DL><p>",
    ]
    return "\n".join(lines) + "\n"


def inject(
    device_id: str, items: list[Bookmark], staging_dir: Path, is_privileged: bool
) -> int:
    """Inject bookmarks into iOS Safari as a Netscape bookmark HTML file.

    The file is pushed to /var/mobile/Media/PhoneTransfer/bookmarks.html via
    standard AFC (no jailbreak required). The user can open it via the Files
    app or import it through Safari's file-sharing mechanism.

    Args:
        device_id: iOS UDID.
        items: Bookmark objects to inject.
        staging_dir: Local directory for temporary files.
        is_privileged: True if the device is jailbroken (not required here).

    Returns:
        len(items) on success, 0 on failure.
    """
    if not items:
        logger.info("No bookmarks to inject for device %s.", device_id)
        return 0

    injector = get_current_injector()
    if injector is not None:
        try:
            count = _inject_via_backup(injector, items)
            logger.info(
                "inject_bookmarks_ios: staged %d bookmark(s) into the backup "
                "for %s", count, device_id,
            )
            return count
        except Exception as exc:
            logger.warning(
                "inject_bookmarks_ios: backup-mod path failed (%s) — "
                "falling back to AFC HTML push", exc,
            )

    staging_dir.mkdir(parents=True, exist_ok=True)
    local_html = staging_dir / "bookmarks.html"

    try:
        html_content = _build_netscape_html(items)
        local_html.write_text(html_content, encoding="utf-8")
        logger.info(
            "Generated Netscape bookmark HTML with %d bookmark(s) at %s",
            len(items),
            local_html,
        )
    except Exception as exc:
        logger.error("Failed to generate bookmark HTML: %s", exc)
        return 0

    try:
        from core.device_connection_cache import get_broker
        from core.afc_connector import AFCConnector

        broker = get_broker(device_id)
        afc = AFCConnector(broker)

        afc.makedirs(_AFC_DEST_DIR)
        afc.write_file(_AFC_DEST_FILE, local_html.read_bytes())
        logger.info(
            "Pushed bookmarks.html to %s on device %s. "
            "Open this file via the Files app on iOS to import bookmarks into Safari.",
            _AFC_DEST_FILE,
            device_id,
        )
        return len(items)

    except Exception as exc:
        logger.error(
            "Failed to push bookmarks HTML to device %s: %s", device_id, exc
        )
        return 0


# ---------------------------------------------------------------------------
# Backup-mod path
# ---------------------------------------------------------------------------

def _inject_via_backup(
    injector: IOSBackupInjector, bookmarks: list[Bookmark]
) -> int:
    db_path = injector.stage_db(_BOOKMARKS_DOMAIN, _BOOKMARKS_RELPATH)

    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=FULL")

        # Confirm the BookmarksBar folder is present.
        row = con.execute(
            "SELECT num_children FROM bookmarks WHERE id=? AND type=?",
            (_BOOKMARKSBAR_ID, _TYPE_FOLDER),
        ).fetchone()
        if not row:
            raise RuntimeError(
                f"Bookmarks.db has no BookmarksBar (id={_BOOKMARKSBAR_ID})"
            )
        parent_num_children = row[0]

        start_order = con.execute(
            "SELECT COALESCE(MAX(order_index), -1) FROM bookmarks WHERE parent=?",
            (_BOOKMARKSBAR_ID,),
        ).fetchone()[0] + 1

        now_apple = time.time() - _APPLE_EPOCH_OFFSET

        inserted = 0
        with con:
            for i, bm in enumerate(bookmarks):
                if not bm.url:
                    continue
                cur = con.execute(
                    """
                    INSERT INTO bookmarks (
                        special_id, parent, type, title, url, num_children,
                        editable, deletable, hidden, hidden_ancestor_count,
                        order_index, external_uuid, read, last_modified,
                        added, deleted,
                        fetched_icon, dav_generation, locally_added,
                        archive_status, syncable, web_filter_status,
                        modified_attributes, subtype
                    ) VALUES (
                        0, ?, ?, ?, ?, 0,
                        1, 1, 0, 0,
                        ?, ?, 0, ?,
                        1, 0,
                        0, 0, 1,
                        0, 1, 0,
                        0, 0
                    )
                    """,
                    (
                        _BOOKMARKSBAR_ID, _TYPE_BOOKMARK,
                        bm.title or bm.url, bm.url,
                        start_order + i,
                        str(uuid.uuid4()).upper(),
                        now_apple,
                    ),
                )
                new_id = cur.lastrowid
                for wi, word in enumerate(_tokenize_title(bm.title or bm.url)):
                    con.execute(
                        "INSERT INTO bookmark_title_words "
                        "(bookmark_id, word, word_index) VALUES (?, ?, ?)",
                        (new_id, word, wi),
                    )
                inserted += 1

            con.execute(
                "UPDATE bookmarks SET num_children=?, last_modified=? WHERE id=?",
                (parent_num_children + inserted, now_apple, _BOOKMARKSBAR_ID),
            )
    finally:
        con.close()

    return inserted


def _tokenize_title(title: str) -> list[str]:
    return [m.group(0).lower() for m in _TITLE_TOKEN_RE.finditer(title)]
