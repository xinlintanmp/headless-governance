"""Team memory stored in Mixpanel's Business Context.

Replaces the local ``memory.md`` for hosted deployments: a container's
filesystem is per-instance and lost on redeploy, and local notes are
invisible to teammates.

``ws.set_business_context`` is a **full replace** (PUT). Writing naively
would destroy whatever policy the team has already written. So every write
here is read-modify-write against a delimited block:

    ...the team's own business context, untouched...

    <!-- agent-memory:start -->
    ## Agent memory
    - **ownership** · ...
    <!-- agent-memory:end -->

Only the text between the markers is ever rewritten.
"""

from __future__ import annotations

import re
from datetime import date

from mixpanel_headless import BUSINESS_CONTEXT_MAX_CHARS

from workspace import ws

START = "<!-- agent-memory:start -->"
END = "<!-- agent-memory:end -->"
HEADING = "## Agent memory"

BLOCK_RE = re.compile(
    re.escape(START) + r".*?" + re.escape(END), re.DOTALL
)

CATEGORIES = ("ownership", "convention", "decision", "exception", "context")

# Leave room so a memory write can never crowd out the team's own content.
SAFETY_MARGIN = 2_000


def _split(content: str) -> tuple[str, str]:
    """Return ``(content_without_block, block_body)``."""
    match = BLOCK_RE.search(content)
    if not match:
        return content, ""
    block = match.group(0)
    body = block[len(START) : -len(END)]
    without = (content[: match.start()] + content[match.end() :]).rstrip()
    return without, body


def read_memory() -> tuple[str, int]:
    """Return ``(block_body, entry_count)`` from the project's context."""
    context = ws.get_business_context(level="project")
    _, body = _split(getattr(context, "content", "") or "")
    return body.strip(), len(re.findall(r"^- ", body, flags=re.MULTILINE))


def append_memory(note: str, category: str | None = None) -> dict[str, object]:
    """Append one entry to the agent-memory block, preserving everything else.

    Read-modify-write. Returns a summary dict and never raises for the
    ordinary cases; library errors propagate to the caller to convert.
    """
    note = " ".join(note.split())
    if not note:
        return {"ok": False, "reason": "empty note"}

    context = ws.get_business_context(level="project")
    current = getattr(context, "content", "") or ""
    without_block, body = _split(current)

    if note.lower() in body.lower():
        return {
            "ok": True,
            "duplicate": True,
            "entry_count": len(re.findall(r"^- ", body, flags=re.MULTILINE)),
            "note": "An entry with this text already exists; nothing was written.",
        }

    label = (category or "").strip().lower()
    prefix = f"**{label}** · " if label else ""
    entry = f"- {prefix}{note}  _(recorded {date.today().isoformat()})_"

    lines = [ln for ln in body.strip().splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("## "):
        lines = [HEADING] + lines
    lines.append(entry)

    new_block = f"{START}\n" + "\n".join(lines) + f"\n{END}"
    new_content = (without_block.rstrip() + "\n\n" + new_block).strip() + "\n"

    limit = BUSINESS_CONTEXT_MAX_CHARS - SAFETY_MARGIN
    if len(new_content) > limit:
        return {
            "ok": False,
            "reason": (
                f"Writing this would bring the project business context to "
                f"{len(new_content):,} characters, over the {limit:,} working limit "
                f"(Mixpanel's hard cap is {BUSINESS_CONTEXT_MAX_CHARS:,}). "
                "Ask the user to prune the agent memory block first."
            ),
        }

    updated = ws.set_business_context(new_content, level="project")
    updated_content = getattr(updated, "content", "") or ""
    _, updated_body = _split(updated_content)

    return {
        "ok": True,
        "duplicate": False,
        "entry": entry,
        "entry_count": len(re.findall(r"^- ", updated_body, flags=re.MULTILINE)),
        "context_chars": len(updated_content),
        "preserved_chars": len(without_block.strip()),
    }
