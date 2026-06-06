"""
convert_bookmarks.py

Converts Bookmark objects to/from Netscape HTML bookmark format and JSON.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from core.normalization_schema import Bookmark

# ---------------------------------------------------------------------------
# HTML (Netscape Bookmark Format)
# ---------------------------------------------------------------------------

_NETSCAPE_HEADER = """\
<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
"""

_NETSCAPE_FOOTER = "</DL><p>\n"


def _html_escape(text: str) -> str:
    """Minimal HTML escaping for attribute values and text content."""
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _unix_ts(dt: datetime | None) -> int:
    """Convert a datetime to a Unix timestamp integer, or 0 if None."""
    if dt is None:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def bookmarks_to_html(bookmarks: list[Bookmark], path: Path) -> Path:
    """
    Write bookmarks to a Netscape Bookmark Format HTML file at *path*.

    Bookmarks are grouped by folder.  Bookmarks with ``folder=None`` are
    written at the top level.  Each folder is wrapped in a ``<DL>`` block
    with a ``<DT><H3>`` header.

    Returns *path*.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Separate into folder groups preserving insertion order
    ungrouped: list[Bookmark] = []
    folders: dict[str, list[Bookmark]] = {}
    for b in bookmarks:
        if b.folder is None:
            ungrouped.append(b)
        else:
            folders.setdefault(b.folder, []).append(b)

    lines: list[str] = [_NETSCAPE_HEADER]

    def write_bookmark(b: Bookmark, indent: str = "    ") -> None:
        ts = _unix_ts(b.added)
        lines.append(
            f'{indent}<DT><A HREF="{_html_escape(b.url)}" ADD_DATE="{ts}">'
            f"{_html_escape(b.title)}</A>\n"
        )

    # Top-level ungrouped bookmarks
    for b in ungrouped:
        write_bookmark(b, indent="    ")

    # Folder groups
    for folder_name, items in folders.items():
        lines.append(f"    <DT><H3>{_html_escape(folder_name)}</H3>\n")
        lines.append("    <DL><p>\n")
        for b in items:
            write_bookmark(b, indent="        ")
        lines.append("    </DL><p>\n")

    lines.append(_NETSCAPE_FOOTER)
    path.write_text("".join(lines), encoding="utf-8")
    return path


def html_to_bookmarks(path: Path) -> list[Bookmark]:
    """
    Parse a Netscape Bookmark Format HTML file and return a list of Bookmark
    objects.

    Uses simple regex matching for ``<A HREF=...>`` tags.  Extracts ``HREF``,
    text content, and ``ADD_DATE`` attribute.  Folder context is tracked via
    ``<H3>`` tags immediately preceding ``<DL>`` blocks.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")

    # Pattern to find H3 folder headers (simplistic but sufficient)
    h3_pattern = re.compile(r"<H3[^>]*>(.*?)</H3>", re.IGNORECASE | re.DOTALL)
    # Pattern for anchor tags
    a_pattern = re.compile(
        r'<A\s+([^>]*)>(.*?)</A>',
        re.IGNORECASE | re.DOTALL,
    )
    href_attr = re.compile(r'HREF\s*=\s*"([^"]*)"', re.IGNORECASE)
    add_date_attr = re.compile(r'ADD_DATE\s*=\s*"(\d+)"', re.IGNORECASE)

    bookmarks: list[Bookmark] = []

    # Walk through the file tracking folder context
    # We'll process the text sequentially using character positions
    current_folder: str | None = None

    # Build a combined list of (position, type, data) events
    events: list[tuple[int, str, str]] = []
    for m in h3_pattern.finditer(text):
        events.append((m.start(), "h3", m.group(1)))
    for m in a_pattern.finditer(text):
        events.append((m.start(), "a", m.group(0)))

    events.sort(key=lambda e: e[0])

    # Track DL depth to reset folder when we exit a folder DL
    # Simple heuristic: when we see a new H3 we update folder; anchors inherit it
    # Opening/closing DL tags reset context when we return to depth 1.
    dl_open_pattern = re.compile(r"<DL", re.IGNORECASE)
    dl_close_pattern = re.compile(r"</DL", re.IGNORECASE)

    # Build position-tagged structure
    struct_events: list[tuple[int, str, str]] = list(events)
    for m in dl_open_pattern.finditer(text):
        struct_events.append((m.start(), "dl_open", ""))
    for m in dl_close_pattern.finditer(text):
        struct_events.append((m.start(), "dl_close", ""))
    struct_events.sort(key=lambda e: e[0])

    depth = 0
    folder_stack: list[str | None] = [None]

    for _, etype, data in struct_events:
        if etype == "dl_open":
            depth += 1
            folder_stack.append(folder_stack[-1])  # inherit current folder
        elif etype == "dl_close":
            if len(folder_stack) > 1:
                folder_stack.pop()
            depth = max(0, depth - 1)
        elif etype == "h3":
            # Next DL will be this folder's container
            folder_name = re.sub(r"<[^>]+>", "", data).strip()
            # Assign to current DL level (the folder_stack top)
            if folder_stack:
                folder_stack[-1] = folder_name
        elif etype == "a":
            attrs_part_match = re.match(r"<A\s+([^>]*)>(.*?)</A>", data, re.IGNORECASE | re.DOTALL)
            if not attrs_part_match:
                continue
            attrs_str = attrs_part_match.group(1)
            raw_title = attrs_part_match.group(2)
            title = re.sub(r"<[^>]+>", "", raw_title).strip()

            href_m = href_attr.search(attrs_str)
            if not href_m:
                continue
            url = href_m.group(1)

            add_date_m = add_date_attr.search(attrs_str)
            added: datetime | None = None
            if add_date_m:
                try:
                    ts = int(add_date_m.group(1))
                    added = datetime.fromtimestamp(ts, tz=timezone.utc)
                except (ValueError, OSError):
                    pass

            current_folder = folder_stack[-1] if folder_stack else None
            bookmarks.append(Bookmark(
                title=title,
                url=url,
                folder=current_folder,
                added=added,
            ))

    return bookmarks


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def bookmark_to_dict(b: Bookmark) -> dict:
    """Serialize a Bookmark to a plain dict (``added`` → ISO string or None)."""
    return {
        "title":  b.title,
        "url":    b.url,
        "folder": b.folder,
        "added":  b.added.isoformat() if b.added else None,
    }


def dict_to_bookmark(d: dict) -> Bookmark:
    """Deserialize a Bookmark from a plain dict."""
    added: datetime | None = None
    if d.get("added"):
        added = datetime.fromisoformat(d["added"])
    return Bookmark(
        title=d["title"],
        url=d["url"],
        folder=d.get("folder"),
        added=added,
    )


def bookmarks_to_json(bookmarks: list[Bookmark], path: Path) -> Path:
    """Write bookmarks to a JSON file at *path*. Returns *path*."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([bookmark_to_dict(b) for b in bookmarks], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def bookmarks_from_json(path: Path) -> list[Bookmark]:
    """Read bookmarks from a JSON file written by :func:`bookmarks_to_json`."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [dict_to_bookmark(d) for d in raw]
