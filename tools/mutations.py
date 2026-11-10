"""Write tools: the only place this agent can change anything in Mixpanel.

Everything else in this package is read-only. Tools here are annotated
``destructive_hint=True`` so the host can prompt before they run, and
``main.py`` adds a terminal confirmation on top of that.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from claude_agent_sdk import tool
from mixpanel_headless import MixpanelHeadlessError, UpdateEventDefinitionParams

from workspace import ws

from ._common import WRITE, as_bool, as_name_list, as_text, error, invalid, jsonable, ok

# Fields UpdateEventDefinitionParams accepts. Anything omitted is left
# unchanged by Mixpanel, so we only ever send what was explicitly supplied.
BOOL_FIELDS = ("verified", "hidden", "dropped", "merged")
TEXT_FIELDS = ("description", "display_name")


@tool(
    "update_event_definition",
    "Update one event's Lexicon definition — its description, display name, tags, or its "
    "verified/hidden/dropped/merged flags. This WRITES to Mixpanel and changes what the "
    "whole project sees, so use it only when the user has clearly asked for that specific "
    "change. Omitted fields are left untouched. Read the current definition with "
    "get_event_definitions first so you can report what changed, and never use it to guess "
    "a fix the user did not ask for.",
    {
        "event_name": Annotated[
            str,
            "Exact name of the single event to update, e.g. 'Signup'. Must match Lexicon "
            "exactly — confirm it with get_event_definitions first.",
        ],
        "description": Annotated[
            str | None,
            "New description text. Omit to leave the description unchanged.",
        ],
        "display_name": Annotated[
            str | None,
            "New human-readable display name. Omit to leave it unchanged.",
        ],
        "tags": Annotated[
            list[str] | None,
            "Replacement list of tags. This REPLACES all existing tags rather than adding "
            "to them, so include the tags you want to keep. Omit to leave tags unchanged.",
        ],
        "verified": Annotated[
            bool | None,
            "Mark the event verified (true) or unverified (false). Omit to leave unchanged.",
        ],
        "hidden": Annotated[
            bool | None,
            "Hide the event from Lexicon and report pickers (true) or unhide it (false). "
            "Omit to leave unchanged.",
        ],
        "dropped": Annotated[
            bool | None,
            "Set the event's dropped flag. WARNING: dropping an event stops Mixpanel "
            "ingesting it, which loses data. Only set this when the user explicitly asks "
            "to drop the event. Omit to leave unchanged.",
        ],
        "merged": Annotated[
            bool | None,
            "Set the event's merged flag. Omit to leave unchanged.",
        ],
    },
    annotations=WRITE,
)
async def update_event_definition(args: dict[str, Any]) -> dict[str, Any]:
    event_name = as_text(args.get("event_name"))
    if not event_name:
        return invalid("'event_name' must be a non-empty event name.")

    changes: dict[str, Any] = {}

    for field in TEXT_FIELDS:
        if args.get(field) is not None:
            changes[field] = as_text(args.get(field))

    for field in BOOL_FIELDS:
        value = as_bool(args.get(field))
        if value is not None:
            changes[field] = value
        elif args.get(field) is not None:
            return invalid(
                f"'{field}' must be true or false, got {args.get(field)!r}."
            )

    if args.get("tags") is not None:
        tags = as_name_list(args.get("tags"))
        if tags is None:
            return invalid(
                "'tags' must be a non-empty list of tag names. Omit it to leave tags "
                "unchanged; there is no way to clear all tags with this tool."
            )
        changes["tags"] = tags

    if not changes:
        return invalid(
            "No fields to update. Supply at least one of: description, display_name, "
            "tags, verified, hidden, dropped, merged."
        )

    try:
        params = UpdateEventDefinitionParams(**changes)
    except Exception as exc:  # pydantic validation
        return error(exc)

    try:
        updated = await asyncio.to_thread(ws.update_event_definition, event_name, params)
    except MixpanelHeadlessError as exc:
        return error(exc)
    except Exception as exc:
        return error(exc)

    return ok(
        {
            "status": "updated",
            "event_name": event_name,
            "fields_changed": sorted(changes),
            "requested_values": jsonable(changes),
            "definition_now": jsonable(updated),
        }
    )
