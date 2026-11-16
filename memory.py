"""Local, file-backed memory for the governance agent.

A plain markdown file (``memory.md``) next to ``main.py``. It is loaded into
the system prompt at startup and appended to by the ``remember`` tool.

This is private to this checkout: it is not sent to Mixpanel and your
teammates do not see it. Durable team-wide policy belongs in Mixpanel's
Business Context instead.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

MEMORY_PATH = Path(__file__).parent / "memory.md"

# Keep the file from growing until it crowds out the rest of the prompt.
MAX_MEMORY_CHARS = 20_000

HEADER = """# Agent memory

Facts this agent has been told to remember about the Mixpanel project.
Newest entries last. Edit or delete anything here by hand.
"""

# Free-form, but a small set keeps entries greppable and the prompt scannable.
CATEGORIES = ("ownership", "convention", "decision", "exception", "context")


def load_memory() -> tuple[str, int]:
    """Return ``(markdown, entry_count)``. Missing file is not an error."""
    if not MEMORY_PATH.exists():
        return "", 0
    try:
        text = MEMORY_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(f"  ! Could not read {MEMORY_PATH.name}: {exc}")
        return "", 0
    if not text:
        return "", 0
    return text, count_entries(text)


def count_entries(text: str) -> int:
    """Entries are top-level bullets, one per remembered fact."""
    return len(re.findall(r"^- ", text, flags=re.MULTILINE))


def append_memory(note: str, category: str | None = None) -> dict[str, object]:
    """Append one entry. Returns a small summary dict, never raises."""
    note = " ".join(note.split())
    if not note:
        return {"ok": False, "reason": "empty note"}

    label = (category or "").strip().lower()
    prefix = f"**{label}** · " if label else ""
    entry = f"- {prefix}{note}  _(recorded {date.today().isoformat()})_\n"

    try:
        existing = MEMORY_PATH.read_text(encoding="utf-8") if MEMORY_PATH.exists() else ""
    except OSError as exc:
        return {"ok": False, "reason": f"could not read memory file: {exc}"}

    if note.lower() in existing.lower():
        return {
            "ok": True,
            "duplicate": True,
            "entry": entry.strip(),
            "entry_count": count_entries(existing),
            "note": "An entry with this text already exists; nothing was added.",
        }

    body = existing if existing.strip() else HEADER + "\n"
    if not body.endswith("\n"):
        body += "\n"
    body += entry

    if len(body) > MAX_MEMORY_CHARS:
        return {
            "ok": False,
            "reason": (
                f"memory.md would exceed {MAX_MEMORY_CHARS} characters. "
                "Ask the user to prune it before adding more."
            ),
        }

    try:
        MEMORY_PATH.write_text(body, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "reason": f"could not write memory file: {exc}"}

    return {
        "ok": True,
        "duplicate": False,
        "entry": entry.strip(),
        "entry_count": count_entries(body),
        "path": MEMORY_PATH.name,
    }
