"""Mixpanel data governance tools for the Claude Agent SDK.

Import ``ALL_TOOLS`` from here and register it with
``create_sdk_mcp_server(name="governance", tools=ALL_TOOLS)``.

Modules
-------
``discovery``  what the data has OBSERVED — event names, properties, volume
``lexicon``    what Lexicon has REGISTERED — definitions, history, metadata
``mutations``  the only tools that change MIXPANEL (see WRITE_TOOL_NAMES)
``memory``     the ``remember`` tool, which writes only to local memory.md
``_common``    shared result shaping, error conversion, argument parsing

The observed/registered split is deliberate: comparing the two is how the
agent finds undocumented events and dead definitions.

Everything except ``mutations`` is read-only. No create, delete, bulk, init,
replace, cancel, upload, download, validate or preview method is exposed.
"""

from __future__ import annotations

from .discovery import (
    list_event_names,
    list_event_properties,
    list_top_events,
)
from .lexicon import (
    export_lexicon,
    get_definition_history,
    get_event_definitions,
    get_property_definitions,
    get_tracking_metadata,
)
from .memory import remember
from .mutations import update_event_definition

# Discovery first: the order is also the order Claude sees them listed.
READ_TOOLS = [
    list_event_names,
    list_event_properties,
    list_top_events,
    get_event_definitions,
    get_property_definitions,
    get_definition_history,
    get_tracking_metadata,
    export_lexicon,
]

WRITE_TOOLS = [
    update_event_definition,
]

# Writes only to the local memory.md file, never to Mixpanel, so it is not
# gated by the terminal confirmation in main.py.
LOCAL_WRITE_TOOLS = [
    remember,
]

ALL_TOOLS = READ_TOOLS + WRITE_TOOLS + LOCAL_WRITE_TOOLS

# Names main.py uses to decide which calls need a human confirmation.
WRITE_TOOL_NAMES = frozenset(t.name for t in WRITE_TOOLS)

__all__ = [
    "ALL_TOOLS",
    "READ_TOOLS",
    "WRITE_TOOLS",
    "WRITE_TOOL_NAMES",
    "list_event_names",
    "list_event_properties",
    "list_top_events",
    "get_event_definitions",
    "get_property_definitions",
    "get_definition_history",
    "get_tracking_metadata",
    "export_lexicon",
    "update_event_definition",
    "remember",
    "LOCAL_WRITE_TOOLS",
]
