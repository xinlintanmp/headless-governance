"""Discovery tools: find out what exists before looking anything up.

These wrap the Mixpanel *query* endpoints, which report what has actually
been **observed** in the data. That is deliberately different from the
Lexicon tools in ``lexicon.py``, which report what has been **registered**.
The gap between the two is itself a governance finding.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from claude_agent_sdk import tool
from mixpanel_headless import MixpanelHeadlessError

from workspace import ws

from ._common import (
    COUNT_TYPES,
    MAX_NAMES,
    READ_ONLY,
    as_positive_int,
    as_text,
    capped,
    error,
    invalid,
)


@tool(
    "list_event_names",
    "List the names of events actually seen in the project's data. This is the primary "
    "discovery tool: call it FIRST whenever you do not know exact event names, then look "
    "up details with get_event_definitions. Note this reflects events OBSERVED in the time "
    "window, not the registered Lexicon schema — an event registered but never fired will "
    "not appear here, which is itself a governance finding. Do NOT use it for descriptions, "
    "tags or verified status (use get_event_definitions).",
    {
        "limit": Annotated[
            int | None,
            "Maximum number of event names to return. Omit for the server maximum (5000).",
        ],
        "from_date": Annotated[
            str | None,
            "Start of the observation window as YYYY-MM-DD. Omit for the widest window "
            "the project allows. Narrow it to find recently active events.",
        ],
        "to_date": Annotated[
            str | None,
            "End of the observation window as YYYY-MM-DD. Omit for today.",
        ],
    },
    annotations=READ_ONLY,
)
async def list_event_names(args: dict[str, Any]) -> dict[str, Any]:
    limit = as_positive_int(args.get("limit"))
    from_date = as_text(args.get("from_date")) or None
    to_date = as_text(args.get("to_date")) or None

    for label, value in (("from_date", from_date), ("to_date", to_date)):
        if value and not _looks_like_date(value):
            return invalid(f"'{label}' must be a date in YYYY-MM-DD format, got {value!r}.")

    try:
        names = await asyncio.to_thread(
            ws.events, limit=limit, from_date=from_date, to_date=to_date
        )
    except MixpanelHeadlessError as exc:
        return error(exc)
    except Exception as exc:
        return error(exc)

    return capped(
        sorted(names),
        limit=MAX_NAMES,
        source="observed_in_data",
        note="These events fired in the window. Registered-but-unfired events are absent.",
        window={"from_date": from_date, "to_date": to_date},
    )


@tool(
    "list_event_properties",
    "List the property names recorded on one specific event. Use it after you know an exact "
    "event name, to discover which properties it carries before looking up their definitions "
    "with get_property_definitions. Do NOT use it to list events (use list_event_names) or to "
    "get property descriptions and sensitivity flags (use get_property_definitions).",
    {
        "event": Annotated[
            str,
            "Exact event name, e.g. 'Signup'. Must match an event in the data; "
            "use list_event_names first if you are unsure.",
        ]
    },
    annotations=READ_ONLY,
)
async def list_event_properties(args: dict[str, Any]) -> dict[str, Any]:
    event = as_text(args.get("event"))
    if not event:
        return invalid("'event' must be a non-empty event name.")

    try:
        names = await asyncio.to_thread(ws.properties, event)
    except MixpanelHeadlessError as exc:
        return error(exc)
    except Exception as exc:
        return error(exc)

    return capped(list(names), limit=MAX_NAMES, event=event, source="observed_in_data")


@tool(
    "list_top_events",
    "List today's highest-volume events with their counts and percent change. Use it to see "
    "which events matter most right now, to prioritise an audit, or to spot a sudden volume "
    "shift. This is real-time and covers today only — do NOT use it as a full event inventory "
    "(use list_event_names) or for historical trends.",
    {
        "type": Annotated[
            str | None,
            "Counting method. Allowed values: 'general', 'average', 'unique'. "
            "Omit for 'general'.",
        ],
        "limit": Annotated[
            int | None,
            "Maximum number of events to return. Omit for the server default.",
        ],
    },
    annotations=READ_ONLY,
)
async def list_top_events(args: dict[str, Any]) -> dict[str, Any]:
    count_type = as_text(args.get("type")).lower() or "general"
    if count_type not in COUNT_TYPES:
        return invalid(
            f"Invalid type {args.get('type')!r}. Allowed values are {list(COUNT_TYPES)}."
        )
    limit = as_positive_int(args.get("limit"))

    try:
        events = await asyncio.to_thread(ws.top_events, type=count_type, limit=limit)
    except MixpanelHeadlessError as exc:
        return error(exc)
    except Exception as exc:
        return error(exc)

    return capped(list(events), type=count_type, window="today", source="observed_in_data")


def _looks_like_date(value: str) -> bool:
    parts = value.split("-")
    return (
        len(parts) == 3
        and len(parts[0]) == 4
        and all(part.isdigit() for part in parts)
    )
