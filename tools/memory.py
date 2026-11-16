"""The ``remember`` tool: write a durable note to local memory.

Writes to ``memory.md`` in the project folder, not to Mixpanel. The file is
loaded into the system prompt at the next startup, so remembered facts
survive restarts.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from claude_agent_sdk import tool

from memory import CATEGORIES, append_memory

from ._common import WRITE, as_text, invalid, ok


@tool(
    "remember",
    "Save one durable fact to local memory so it survives restarts — ownership, naming "
    "conventions, decisions, or agreed exceptions about this project's tracking. Use it "
    "when the user tells you something worth keeping ('the payments team owns checkout "
    "events', 'legacy_ prefixed events are deprecated'), or explicitly says to remember "
    "something. Do NOT use it for facts you can look up with a tool, for one-off answers, "
    "or to store findings from an audit — those change, so re-read them instead. This "
    "writes to a local file only; it does not change anything in Mixpanel.",
    {
        "note": Annotated[
            str,
            "The fact to remember, as one self-contained sentence. It must make sense "
            "months later with no conversation around it: write 'The payments team owns "
            "all checkout_* events' rather than 'they own those'.",
        ],
        "category": Annotated[
            str | None,
            "Optional label. Suggested values: 'ownership', 'convention', 'decision', "
            "'exception', 'context'. Omit if none fits.",
        ],
    },
    annotations=WRITE,
)
async def remember(args: dict[str, Any]) -> dict[str, Any]:
    note = as_text(args.get("note"))
    if not note:
        return invalid("'note' must be a non-empty sentence to remember.")
    if len(note) > 500:
        return invalid(
            f"'note' is {len(note)} characters. Keep each memory to one sentence "
            "(under 500 characters); split longer material into several notes."
        )

    category = as_text(args.get("category")).lower() or None

    result = await asyncio.to_thread(append_memory, note, category)

    if not result.get("ok"):
        return invalid(str(result.get("reason", "could not write memory")))

    if result.get("duplicate"):
        return ok(
            {
                "status": "already_known",
                "entry": result.get("entry"),
                "entry_count": result.get("entry_count"),
                "suggested_categories": list(CATEGORIES),
            }
        )

    return ok(
        {
            "status": "remembered",
            "entry": result.get("entry"),
            "entry_count": result.get("entry_count"),
            "file": result.get("path"),
            "note": "Saved locally. It loads into context at the next startup.",
        }
    )
