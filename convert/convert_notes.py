"""
convert_notes.py

Converts Note objects to/from plain text and simple HTML.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from core.normalization_schema import Note

# Characters that are illegal in most filesystem paths
_ILLEGAL_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|]')
_MAX_FILENAME_LEN = 80


# ---------------------------------------------------------------------------
# Plain-text helpers
# ---------------------------------------------------------------------------

def note_to_text(note: Note) -> str:
    """
    Render a Note as a plain-text string.

    Format::

        # {title}

        {body}
    """
    return f"# {note.title}\n\n{note.body}"


def text_to_note(
    text: str,
    created: datetime | None = None,
) -> Note:
    """
    Parse a plain-text string (written by :func:`note_to_text`) into a Note.

    - If the first line starts with ``#``, it is used as the title (the ``#``
      and surrounding whitespace are stripped).
    - Otherwise the title is the first 60 characters of the first line.
    - The body is everything after the first line (leading blank line stripped).
    """
    lines = text.split("\n")
    first_line = lines[0] if lines else ""

    if first_line.startswith("#"):
        title = first_line.lstrip("#").strip()
        body_lines = lines[1:]
        # Drop a single leading blank line produced by note_to_text
        if body_lines and body_lines[0].strip() == "":
            body_lines = body_lines[1:]
        body = "\n".join(body_lines)
    else:
        title = first_line[:60].strip()
        body = "\n".join(lines[1:]) if len(lines) > 1 else ""

    now = created or datetime.now(tz=timezone.utc)
    return Note(
        title=title,
        body=body,
        created=now,
        modified=now,
        folder=None,
    )


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def note_to_html(note: Note) -> str:
    """
    Render a Note as minimal HTML.

    The title is wrapped in ``<h1>``.  Newlines in the body are converted to
    ``<br>`` tags.
    """
    escaped_title = note.title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    escaped_body = (
        note.body
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>\n")
    )
    return (
        f"<!DOCTYPE html>\n<html>\n<body>\n"
        f"<h1>{escaped_title}</h1>\n"
        f"<p>{escaped_body}</p>\n"
        f"</body>\n</html>"
    )


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def _sanitize_filename(title: str, max_len: int = _MAX_FILENAME_LEN) -> str:
    """Replace illegal filesystem characters with underscores, then truncate."""
    sanitized = _ILLEGAL_FILENAME_CHARS.sub("_", title)
    sanitized = sanitized.strip(". ")  # avoid hidden files and trailing dots
    sanitized = sanitized or "untitled"
    return sanitized[:max_len]


def notes_to_text_files(notes: list[Note], directory: Path) -> list[Path]:
    """
    Write each Note to a ``.txt`` file inside *directory*.

    Filename = ``{sanitized_title}.txt`` (max 80 chars before extension).
    Collisions are resolved by appending ``_2``, ``_3``, etc.

    Returns a list of Path objects for all written files.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    used_names: dict[str, int] = {}
    written: list[Path] = []

    for note in notes:
        base = _sanitize_filename(note.title)
        if base in used_names:
            used_names[base] += 1
            filename = f"{base}_{used_names[base]}.txt"
        else:
            used_names[base] = 1
            # Check if file already exists on disk
            candidate = directory / f"{base}.txt"
            if candidate.exists():
                used_names[base] = 2
                filename = f"{base}_2.txt"
            else:
                filename = f"{base}.txt"

        path = directory / filename
        path.write_text(note_to_text(note), encoding="utf-8")
        written.append(path)

    return written


def notes_from_text_files(directory: Path) -> list[Note]:
    """
    Read all ``.txt`` files in *directory* and parse each into a Note.

    Uses the ``# Title`` format produced by :func:`note_to_text`.  Falls back
    to a best-guess parse for arbitrary text files.

    Returns a list of Note objects.
    """
    directory = Path(directory)
    notes: list[Note] = []
    for txt_file in sorted(directory.glob("*.txt")):
        try:
            text = txt_file.read_text(encoding="utf-8", errors="replace")
            stat = txt_file.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            note = text_to_note(text, created=mtime)
            note = Note(
                title=note.title,
                body=note.body,
                created=mtime,
                modified=mtime,
                folder=None,
            )
            notes.append(note)
        except Exception:
            pass
    return notes


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def note_to_dict(note: Note) -> dict:
    """Serialize a Note to a plain dict (``datetime`` fields → ISO strings)."""
    return {
        "title":    note.title,
        "body":     note.body,
        "created":  note.created.isoformat() if note.created else None,
        "modified": note.modified.isoformat() if note.modified else None,
        "folder":   note.folder,
    }


def dict_to_note(d: dict) -> Note:
    """Deserialize a Note from a plain dict."""
    def _parse_dt(v: str | None) -> datetime | None:
        if v is None:
            return None
        try:
            return datetime.fromisoformat(v)
        except (ValueError, TypeError):
            return None

    return Note(
        title=d["title"],
        body=d["body"],
        created=_parse_dt(d.get("created")),
        modified=_parse_dt(d.get("modified")),
        folder=d.get("folder"),
    )


def notes_to_json(notes: list[Note], path: Path) -> Path:
    """Write notes to a JSON file. Returns *path*."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([note_to_dict(n) for n in notes], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def notes_from_json(path: Path) -> list[Note]:
    """Read notes from a JSON file written by :func:`notes_to_json`."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [dict_to_note(d) for d in raw]
