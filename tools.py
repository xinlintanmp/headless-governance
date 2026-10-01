"""Read-only Mixpanel data governance tools for the Claude Agent SDK.

Every tool wraps a read-only ``mixpanel_headless`` Workspace method. The
blocking headless call runs in a worker thread so the asyncio event loop
stays responsive, results are returned as compact JSON, and every
``MixpanelHeadlessError`` is converted into an error result the model can
read and correct from rather than an exception that kills the agent.

No mutating method (create / update / delete / bulk / init / replace /
cancel / upload / download / validate / preview) is exposed here.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from claude_agent_sdk import tool
from mcp_types import ToolAnnotations
from mixpanel_headless import MixpanelHeadlessError

from workspace import ws

# Maximum number of list items returned by any single tool call.
MAX_ITEMS = 50

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

RESOURCE_TYPES = ("event", "user", "groupprofile")
ENTITY_TYPES = ("event", "user", "group")
EXPORT_TYPES = ("All Events and Properties", "All User Profile Properties")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Convert pydantic models / arbitrary objects into JSON-safe data."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _ok(payload: Any) -> dict[str, Any]:
    """A successful tool result carrying compact JSON."""
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, separators=(",", ":"), default=str)}
        ]
    }


def _capped(items: list[Any], **extra: Any) -> dict[str, Any]:
    """Cap a list at MAX_ITEMS and report the true total."""
    data = [_jsonable(item) for item in items[:MAX_ITEMS]]
    payload: dict[str, Any] = {
        "total_count": len(items),
        "returned_count": len(data),
        "truncated": len(items) > MAX_ITEMS,
        "items": data,
    }
    payload.update(extra)
    return _ok(payload)


def _error(exc: Exception) -> dict[str, Any]:
    """Turn an exception into an error result Claude can act on."""
    if isinstance(exc, MixpanelHeadlessError):
        text = json.dumps(
            {
                "error": type(exc).__name__,
                "code": getattr(exc, "code", "UNKNOWN_ERROR"),
                "message": getattr(exc, "message", str(exc)),
                "details": _jsonable(getattr(exc, "details", None)),
            },
            separators=(",", ":"),
            default=str,
        )
    else:
        text = json.dumps(
            {"error": type(exc).__name__, "message": str(exc)},
            separators=(",", ":"),
            default=str,
        )
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _invalid(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps({"error": "InvalidArgument", "message": message})}],
        "is_error": True,
    }


def _as_name_list(value: Any, field: str) -> list[str] | None:
    """Accept a list of names, or a single name string, or return None."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        return None
    names = [str(v).strip() for v in value if str(v).strip()]
    return names or None


# --------------------------------------------------------------------------
# 1. export_lexicon as a CSV
# --------------------------------------------------------------------------


@tool(
    "export_lexicon",
    "Export the whole Mixpanel Lexicon — every tracked event and property definition "
    "in the workspace — for discovery, audits, or finding the exact spelling of a name. "
    "Use this only when the user requests a CSV export of the events to their email.", 
    # "Use this FIRST whenever you do not already know exact event or property names, then "
    # "look up specifics with get_event_definitions or get_property_definitions. Do NOT use "
    # "it when you already have exact names (the targeted tools are far smaller), and do not "
    # "use it to fetch change history or tracking volume.",
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
        export_types = _as_name_list(export_types, "export_types")
        if export_types is None:
            return _invalid(
                f"'export_types' must be a non-empty list of strings from {list(EXPORT_TYPES)}, "
                "or omitted to export everything."
            )
        bad = [t for t in export_types if t not in EXPORT_TYPES]
        if bad:
            return _invalid(
                f"Invalid export_types {bad}. Allowed values are {list(EXPORT_TYPES)}."
            )
    try:
        export = await asyncio.to_thread(ws.export_lexicon, export_types=export_types)
    except MixpanelHeadlessError as exc:
        return _error(exc)
    except Exception as exc:  # never let the agent crash
        return _error(exc)

    # The export is a dict of name -> list of definitions; cap each list.
    summary: dict[str, Any] = {"export_types": export_types or list(EXPORT_TYPES)}
    sections: dict[str, Any] = {}
    truncated = False
    total = 0
    for key, value in (export or {}).items():
        if isinstance(value, list):
            total += len(value)
            truncated = truncated or len(value) > MAX_ITEMS
            sections[str(key)] = {
                "total_count": len(value),
                "returned_count": min(len(value), MAX_ITEMS),
                "truncated": len(value) > MAX_ITEMS,
                "items": [_jsonable(v) for v in value[:MAX_ITEMS]],
            }
        else:
            sections[str(key)] = _jsonable(value)
    summary["sections"] = sections
    summary["total_count"] = total
    summary["truncated"] = truncated
    return _ok(summary)


# --------------------------------------------------------------------------
# 2. get_event_definitions
# --------------------------------------------------------------------------


@tool(
    "get_event_definitions",
    "Look up Lexicon governance metadata for specific events by event name — description, "
    "display name, tags, verified/hidden/dropped status, platforms, and modification dates. "
    "Use it to audit whether named events are documented, verified, or deprecated.  "
    "You can guess names, event names can also be case insensitive, and you can pass multiple names at once. ",
    {
        "names": Annotated[
            list[str],
            "Exact event names to look up, e.g. ['Signup', 'Login']. Names are "
            "case-sensitive and must match Lexicon exactly.",
        ]
    },
    annotations=READ_ONLY,
)
async def get_event_definitions(args: dict[str, Any]) -> dict[str, Any]:
    names = _as_name_list(args.get("names"), "names")
    if names is None:
        return _invalid("'names' must be a non-empty list of exact event names.")
    try:
        definitions = await asyncio.to_thread(ws.get_event_definitions, names=names)
    except MixpanelHeadlessError as exc:
        return _error(exc)
    except Exception as exc:
        return _error(exc)

    found = {getattr(d, "name", None) for d in definitions}
    return _capped(
        list(definitions),
        requested_count=len(names),
        not_found=[n for n in names if n not in found],
    )


# --------------------------------------------------------------------------
# 3. get_property_definitions
# --------------------------------------------------------------------------


@tool(
    "get_property_definitions",
    "Look up Lexicon governance metadata for specific properties by exact name — "
    "description, display name, example value, resource type, and hidden/dropped/sensitive "
    "flags. Use it to audit whether named properties are documented or flagged sensitive, "
    "optionally narrowing to one resource type. Do NOT guess names: run export_lexicon "
    "first if you are unsure of the exact spelling, and use get_event_definitions for "
    "events rather than properties.",
    {
        "names": Annotated[
            list[str],
            "Exact property names to look up, e.g. ['plan_type', 'country']. "
            "Case-sensitive, must match Lexicon exactly.",
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
    names = _as_name_list(args.get("names"), "names")
    if names is None:
        return _invalid("'names' must be a non-empty list of exact property names.")

    resource_type = args.get("resource_type")
    if resource_type is not None:
        resource_type = str(resource_type).strip().lower()
        if resource_type not in RESOURCE_TYPES:
            return _invalid(
                f"Invalid resource_type '{args.get('resource_type')}'. "
                f"Allowed values are {list(RESOURCE_TYPES)}, or omit it to search all."
            )
    try:
        definitions = await asyncio.to_thread(
            ws.get_property_definitions, names=names, resource_type=resource_type
        )
    except MixpanelHeadlessError as exc:
        return _error(exc)
    except Exception as exc:
        return _error(exc)

    found = {getattr(d, "name", None) for d in definitions}
    return _capped(
        list(definitions),
        requested_count=len(names),
        resource_type=resource_type,
        not_found=[n for n in names if n not in found],
    )


# --------------------------------------------------------------------------
# 4. get_definition_history
# --------------------------------------------------------------------------


@tool(
    "get_definition_history",
    "Get the change history of a single event or property definition — who changed the "
    "Lexicon entry, when, and what changed. Use it to investigate when an event was "
    "deprecated, renamed, or last documented. Do NOT use it to read the current definition "
    "(get_event_definitions / get_property_definitions) or to discover which names exist "
    "(export_lexicon); it accepts exactly one name per call.",
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
    kind = str(args.get("kind", "")).strip().lower()
    if kind not in ("event", "property"):
        return _invalid(
            f"Invalid kind '{args.get('kind')}'. Allowed values are ['event', 'property']."
        )

    name = args.get("name")
    name = str(name).strip() if name is not None else ""
    if not name:
        return _invalid("'name' must be a non-empty event or property name.")

    entity_type = args.get("entity_type")
    if kind == "property":
        if entity_type is None or not str(entity_type).strip():
            return _invalid(
                "'entity_type' is required when kind='property'. "
                f"Allowed values are {list(ENTITY_TYPES)}."
            )
        entity_type = str(entity_type).strip().lower()
        if entity_type not in ENTITY_TYPES:
            return _invalid(
                f"Invalid entity_type '{args.get('entity_type')}'. "
                f"Allowed values are {list(ENTITY_TYPES)}."
            )

    try:
        if kind == "event":
            history = await asyncio.to_thread(ws.get_event_history, name)
        else:
            history = await asyncio.to_thread(ws.get_property_history, name, entity_type)
    except MixpanelHeadlessError as exc:
        return _error(exc)
    except Exception as exc:
        return _error(exc)

    extra: dict[str, Any] = {"kind": kind, "name": name}
    if kind == "property":
        extra["entity_type"] = entity_type
    return _capped(list(history), **extra)


# --------------------------------------------------------------------------
# 5. get_tracking_metadata
# --------------------------------------------------------------------------


@tool(
    "get_tracking_metadata",
    "Get tracking metadata for one event — how it is actually being sent, such as sources, "
    "SDKs, and volume. Use it to check whether a documented event is still live, which "
    "platforms emit it, or whether it looks stale. Do NOT use it for Lexicon documentation "
    "fields like description or tags (get_event_definitions), for properties, or to "
    "discover event names (export_lexicon).",
    {
        "event_name": Annotated[
            str,
            "Exact name of a single event, e.g. 'Signup'. Case-sensitive; must match "
            "Lexicon exactly.",
        ]
    },
    annotations=READ_ONLY,
)
async def get_tracking_metadata(args: dict[str, Any]) -> dict[str, Any]:
    event_name = args.get("event_name")
    event_name = str(event_name).strip() if event_name is not None else ""
    if not event_name:
        return _invalid("'event_name' must be a non-empty event name.")
    try:
        metadata = await asyncio.to_thread(ws.get_tracking_metadata, event_name)
    except MixpanelHeadlessError as exc:
        return _error(exc)
    except Exception as exc:
        return _error(exc)

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
                "items": [_jsonable(v) for v in value[:MAX_ITEMS]],
            }
        else:
            fields[str(key)] = _jsonable(value)
    payload["metadata"] = fields
    payload["total_count"] = len(fields)
    payload["truncated"] = truncated
    return _ok(payload)


# --------------------------------------------------------------------------
# Exported tool list for agent.py
# --------------------------------------------------------------------------

ALL_TOOLS = [
    export_lexicon,
    get_event_definitions,
    get_property_definitions,
    get_definition_history,
    get_tracking_metadata,
]
