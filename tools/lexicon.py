"""Lexicon tools: the **registered** governance metadata for events and properties.

These report what has been documented in Lexicon, as opposed to the
discovery tools, which report what has been observed in the data.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from claude_agent_sdk import tool
from mixpanel_headless import MixpanelHeadlessError

from workspace import ws

from ._common import (
    ENTITY_TYPES,
    EXPORT_TYPES,
    MAX_ITEMS,
    READ_ONLY,
    RESOURCE_TYPES,
    as_name_list,
    as_text,
    capped,
    error,
    invalid,
    jsonable,
    ok,
)


# --------------------------------------------------------------------------
# 1. export_lexicon — asynchronous CSV export, delivered by email
# --------------------------------------------------------------------------


@tool(
    "export_lexicon",
    "Request a CSV export of the whole Lexicon, which Mixpanel emails to the account "
    "owner. Use this ONLY when the user explicitly asks for a CSV or an emailed export. "
    "It is asynchronous and returns NO data to you, so it is useless for answering "
    "questions — to discover event names use list_event_names, and for definitions use "
    "get_event_definitions or get_property_definitions.",
    {
        "export_types": Annotated[
            list[str] | None,
            "Optional filter. Allowed values: 'All Events and Properties', "
            "'All User Profile Properties'. Omit or pass null to export both.",
        ]
    },
    annotations=READ_ONLY,
)
async def export_lexicon(args: dict[str, Any]) -> dict[str, Any]:
    export_types = args.get("export_types")
    if export_types is not None:
        export_types = as_name_list(export_types)
        if export_types is None:
            return invalid(
                f"'export_types' must be a non-empty list of strings from {list(EXPORT_TYPES)}, "
                "or omitted to export everything."
            )
        bad = [t for t in export_types if t not in EXPORT_TYPES]
        if bad:
            return invalid(
                f"Invalid export_types {bad}. Allowed values are {list(EXPORT_TYPES)}."
            )
    try:
        export = await asyncio.to_thread(ws.export_lexicon, export_types=export_types)
    except MixpanelHeadlessError as exc:
        return error(exc)
    except Exception as exc:  # never let the agent crash
        return error(exc)

    requested = export_types or list(EXPORT_TYPES)
    export = export or {}

    # Mixpanel answers this endpoint asynchronously: it returns a status
    # string rather than the data, and the library wraps that as
    # {"status": "pending", ...}. There is no job id and nothing to poll,
    # so say so plainly instead of letting Claude retry forever.
    if export.get("status") == "pending":
        return ok(
            {
                "status": "accepted_async",
                "retryable": False,
                "export_types": requested,
                "message": str(export.get("message", "")),
                "what_happens_next": (
                    "Mixpanel has queued the export and will deliver it by email to the "
                    "account owner. It is not returned here and there is no status to poll."
                ),
                "do_not": (
                    "Do not call export_lexicon again to wait for this. For event names "
                    "call list_event_names; for definitions call get_event_definitions."
                ),
            }
        )

    # Defensive: if this project returns the data inline, shape it as before.
    sections: dict[str, Any] = {}
    truncated = False
    total = 0
    for key, value in export.items():
        if isinstance(value, list):
            total += len(value)
            truncated = truncated or len(value) > MAX_ITEMS
            sections[str(key)] = {
                "total_count": len(value),
                "returned_count": min(len(value), MAX_ITEMS),
                "truncated": len(value) > MAX_ITEMS,
                "items": [jsonable(v) for v in value[:MAX_ITEMS]],
            }
        else:
            sections[str(key)] = jsonable(value)

    return ok(
        {
            "status": "returned_inline",
            "export_types": requested,
            "sections": sections,
            "total_count": total,
            "truncated": truncated,
        }
    )


# --------------------------------------------------------------------------
# 2. get_event_definitions
# --------------------------------------------------------------------------


@tool(
    "get_event_definitions",
    "Look up Lexicon governance metadata for events by name — description, display name, "
    "tags, verified/hidden/dropped status, platforms and modification dates. Use it to audit "
    "whether events are documented, verified or deprecated; you can pass many names at once. "
    "Prefer list_event_names first to get exact spellings — names not found come back in "
    "'not_found' rather than as an error. Do NOT use it for properties "
    "(get_property_definitions) or for how an event is sent (get_tracking_metadata).",
    {
        "names": Annotated[
            list[str],
            "Event names to look up, e.g. ['Signup', 'Login']. Best results with exact "
            "Lexicon spelling; use list_event_names if unsure.",
        ]
    },
    annotations=READ_ONLY,
)
async def get_event_definitions(args: dict[str, Any]) -> dict[str, Any]:
    names = as_name_list(args.get("names"))
    if names is None:
        return invalid("'names' must be a non-empty list of event names.")
    try:
        definitions = await asyncio.to_thread(ws.get_event_definitions, names=names)
    except MixpanelHeadlessError as exc:
        return error(exc)
    except Exception as exc:
        return error(exc)

    found = {getattr(d, "name", None) for d in definitions}
    return capped(
        list(definitions),
        requested_count=len(names),
        not_found=[n for n in names if n not in found],
        source="registered_in_lexicon",
    )


# --------------------------------------------------------------------------
# 3. get_property_definitions
# --------------------------------------------------------------------------


@tool(
    "get_property_definitions",
    "Look up Lexicon governance metadata for properties by name — description, display "
    "name, example value, resource type and hidden/dropped/sensitive flags. Use it to audit "
    "whether properties are documented or flagged sensitive, optionally narrowed to one "
    "resource type. Use list_event_properties first to discover which properties an event "
    "carries; use get_event_definitions for events rather than properties.",
    {
        "names": Annotated[
            list[str],
            "Property names to look up, e.g. ['plan_type', 'country'].",
        ],
        "resource_type": Annotated[
            str | None,
            "Optional filter for which kind of property. Allowed values: "
            "'event', 'user', 'groupprofile'. Omit to search all resource types.",
        ],
    },
    annotations=READ_ONLY,
)
async def get_property_definitions(args: dict[str, Any]) -> dict[str, Any]:
    names = as_name_list(args.get("names"))
    if names is None:
        return invalid("'names' must be a non-empty list of property names.")

    resource_type = args.get("resource_type")
    if resource_type is not None:
        resource_type = as_text(resource_type).lower()
        if resource_type not in RESOURCE_TYPES:
            return invalid(
                f"Invalid resource_type {args.get('resource_type')!r}. "
                f"Allowed values are {list(RESOURCE_TYPES)}, or omit it to search all."
            )
    try:
        definitions = await asyncio.to_thread(
            ws.get_property_definitions, names=names, resource_type=resource_type
        )
    except MixpanelHeadlessError as exc:
        return error(exc)
    except Exception as exc:
        return error(exc)

    found = {getattr(d, "name", None) for d in definitions}
    return capped(
        list(definitions),
        requested_count=len(names),
        resource_type=resource_type,
        not_found=[n for n in names if n not in found],
        source="registered_in_lexicon",
    )


# --------------------------------------------------------------------------
# 4. get_definition_history
# --------------------------------------------------------------------------


@tool(
    "get_definition_history",
    "Get the change history of a single event or property definition — who changed the "
    "Lexicon entry, when, and what changed. Use it to investigate when something was "
    "deprecated, renamed or last documented. Do NOT use it to read the current definition "
    "(get_event_definitions / get_property_definitions) or to discover names "
    "(list_event_names); it accepts exactly one name per call.",
    {
        "kind": Annotated[
            str,
            "Which definition's history to read. Allowed values: 'event', 'property'.",
        ],
        "name": Annotated[
            str,
            "Exact name of the single event or property, e.g. 'Signup' or 'plan_type'.",
        ],
        "entity_type": Annotated[
            str | None,
            "Required when kind='property': which entity the property belongs to. "
            "Allowed values: 'event', 'user', 'group'. Ignored when kind='event'.",
        ],
    },
    annotations=READ_ONLY,
)
async def get_definition_history(args: dict[str, Any]) -> dict[str, Any]:
    kind = as_text(args.get("kind")).lower()
    if kind not in ("event", "property"):
        return invalid(
            f"Invalid kind {args.get('kind')!r}. Allowed values are ['event', 'property']."
        )

    name = as_text(args.get("name"))
    if not name:
        return invalid("'name' must be a non-empty event or property name.")

    entity_type = args.get("entity_type")
    if kind == "property":
        if entity_type is None or not as_text(entity_type):
            return invalid(
                "'entity_type' is required when kind='property'. "
                f"Allowed values are {list(ENTITY_TYPES)}."
            )
        entity_type = as_text(entity_type).lower()
        if entity_type not in ENTITY_TYPES:
            return invalid(
                f"Invalid entity_type {args.get('entity_type')!r}. "
                f"Allowed values are {list(ENTITY_TYPES)}."
            )

    try:
        if kind == "event":
            history = await asyncio.to_thread(ws.get_event_history, name)
        else:
            history = await asyncio.to_thread(ws.get_property_history, name, entity_type)
    except MixpanelHeadlessError as exc:
        return error(exc)
    except Exception as exc:
        return error(exc)

    extra: dict[str, Any] = {"kind": kind, "name": name}
    if kind == "property":
        extra["entity_type"] = entity_type
    return capped(list(history), **extra)


# --------------------------------------------------------------------------
# 5. get_tracking_metadata
# --------------------------------------------------------------------------


@tool(
    "get_tracking_metadata",
    "Get tracking metadata for one event — how it is actually being sent, such as sources, "
    "SDKs and volume. Use it to check whether a documented event is still live, which "
    "platforms emit it, or whether it looks stale. Do NOT use it for Lexicon documentation "
    "fields like description or tags (get_event_definitions), for properties, or to "
    "discover event names (list_event_names).",
    {
        "event_name": Annotated[
            str,
            "Exact name of a single event, e.g. 'Signup'.",
        ]
    },
    annotations=READ_ONLY,
)
async def get_tracking_metadata(args: dict[str, Any]) -> dict[str, Any]:
    event_name = as_text(args.get("event_name"))
    if not event_name:
        return invalid("'event_name' must be a non-empty event name.")
    try:
        metadata = await asyncio.to_thread(ws.get_tracking_metadata, event_name)
    except MixpanelHeadlessError as exc:
        return error(exc)
    except Exception as exc:
        return error(exc)

    payload: dict[str, Any] = {"event_name": event_name}
    truncated = False
    fields: dict[str, Any] = {}
    for key, value in (metadata or {}).items():
        if isinstance(value, list):
            truncated = truncated or len(value) > MAX_ITEMS
            fields[str(key)] = {
                "total_count": len(value),
                "returned_count": min(len(value), MAX_ITEMS),
                "truncated": len(value) > MAX_ITEMS,
                "items": [jsonable(v) for v in value[:MAX_ITEMS]],
            }
        else:
            fields[str(key)] = jsonable(value)
    payload["metadata"] = fields
    payload["total_count"] = len(fields)
    payload["truncated"] = truncated
    return ok(payload)
