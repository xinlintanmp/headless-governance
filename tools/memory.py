"""The ``remember`` tool.

Writes to Mixpanel's project Business Context, inside a delimited
``agent-memory`` block, so remembered facts are shared with the team,
survive redeploys, and are visible in Mixpanel itself.

This WRITES to Mixpanel, so it is gated by the approval prompt like any
other Mixpanel write. The write is read-modify-write: nothing outside the
agent-memory block is ever touched.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from claude_agent_sdk import tool
from mixpanel_headless import MixpanelHeadlessError

from team_memory import CATEGORIES, append_memory, read_memory

from ._common import WRITE, as_text, error, invalid, ok


@tool(
    "remember",
    "Save one durable fact to the team's shared memory — ownership, naming conventions, "
    "decisions, agreed exceptions about this project's tracking. Use it when the user "
    "tells you something worth keeping ('the payments team owns checkout events') or says "
    "to remember something. Do NOT use it for anything a tool can look up, for audit "
    "findings or counts (they go stale), or for one-off answers. This WRITES to Mixpanel's "
    "project Business Context, so the whole team sees it and it needs approval.",
    {
        "note": Annotated[
            str,
            "The fact to remember, as one self-contained sentence that will still make "
            "sense months later with no surrounding conversation: 'The payments team owns "
            "all checkout_* events', not 'they own those'.",
        ],
        "category": Annotated[
            str | None,
            "Optional label. Suggested: 'ownership', 'convention', 'decision', "
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

    try:
        result = await asyncio.to_thread(append_memory, note, category)
    except MixpanelHeadlessError as exc:
        return error(exc)
    except Exception as exc:
        return error(exc)

    if not result.get("ok"):
        return invalid(str(result.get("reason", "could not write memory")))

    if result.get("duplicate"):
        return ok(
            {
                "status": "already_known",
                "entry_count": result.get("entry_count"),
                "suggested_categories": list(CATEGORIES),
            }
        )

    return ok(
        {
            "status": "remembered",
            "entry": result.get("entry"),
            "entry_count": result.get("entry_count"),
            "stored_in": "Mixpanel project Business Context (agent-memory block)",
            "context_chars": result.get("context_chars"),
            "preserved_chars": result.get("preserved_chars"),
            "note": "Visible to the whole team. Loads into context at the next startup.",
        }
    )


@tool(
    "recall_memory",
    "Read back the team's shared agent memory from Mixpanel Business Context. Use it when "
    "the user asks what you remember, or when you need to check whether something is "
    "already recorded before calling remember. The memory is already in your system prompt "
    "from startup, so only call this if facts may have changed during this session.",
    {},
    annotations=None,
)
async def recall_memory(args: dict[str, Any]) -> dict[str, Any]:
    try:
        body, count = await asyncio.to_thread(read_memory)
    except MixpanelHeadlessError as exc:
        return error(exc)
    except Exception as exc:
        return error(exc)
    return ok({"entry_count": count, "markdown": body or "(nothing remembered yet)"})
