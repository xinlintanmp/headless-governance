"""Shared helpers for the governance tools.

Result shaping, JSON serialization, error conversion and argument
normalization live here so every tool module behaves identically.
"""

from __future__ import annotations

import json
from typing import Any

from mcp_types import ToolAnnotations
from mixpanel_headless import MixpanelHeadlessError

# Maximum number of list items returned by any single tool call.
MAX_ITEMS = 50

# Discovery tools return bare name strings, which are far cheaper per item
# than full definition objects, so they get a higher cap.
MAX_NAMES = 500

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

# Tools that change Mixpanel. The host may prompt before running these, and
# main.py adds its own terminal confirmation.
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

RESOURCE_TYPES = ("event", "user", "groupprofile")
ENTITY_TYPES = ("event", "user", "group")
EXPORT_TYPES = ("All Events and Properties", "All User Profile Properties")
COUNT_TYPES = ("general", "average", "unique")


def jsonable(value: Any) -> Any:
    """Convert pydantic models / arbitrary objects into JSON-safe data."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dataclass_fields__"):
        return {f: jsonable(getattr(value, f)) for f in value.__dataclass_fields__}
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def ok(payload: Any) -> dict[str, Any]:
    """A successful tool result carrying compact JSON."""
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, separators=(",", ":"), default=str)}
        ]
    }


def capped(items: list[Any], *, limit: int = MAX_ITEMS, **extra: Any) -> dict[str, Any]:
    """Cap a list and report the true total so Claude can say it was truncated."""
    data = [jsonable(item) for item in items[:limit]]
    payload: dict[str, Any] = {
        "total_count": len(items),
        "returned_count": len(data),
        "truncated": len(items) > limit,
        "items": data,
    }
    payload.update(extra)
    return ok(payload)


def error(exc: Exception) -> dict[str, Any]:
    """Turn an exception into an error result Claude can act on."""
    if isinstance(exc, MixpanelHeadlessError):
        text = json.dumps(
            {
                "error": type(exc).__name__,
                "code": getattr(exc, "code", "UNKNOWN_ERROR"),
                "message": getattr(exc, "message", str(exc)),
                "details": jsonable(getattr(exc, "details", None)),
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


def invalid(message: str) -> dict[str, Any]:
    """An argument-validation failure, phrased so Claude can correct and retry."""
    return {
        "content": [
            {"type": "text", "text": json.dumps({"error": "InvalidArgument", "message": message})}
        ],
        "is_error": True,
    }


def as_name_list(value: Any) -> list[str] | None:
    """Accept a list of names, or a single name string, or return None."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        return None
    names = [str(v).strip() for v in value if str(v).strip()]
    return names or None


def as_text(value: Any) -> str:
    """Normalize a single required string argument."""
    return str(value).strip() if value is not None else ""


def as_bool(value: Any) -> bool | None:
    """Return a real bool, accepting the strings models sometimes emit."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1"):
            return True
        if lowered in ("false", "no", "0"):
            return False
    return None


def as_positive_int(value: Any) -> int | None:
    """Return a positive int, or None when absent/unparseable."""
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
