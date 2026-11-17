"""Web server for the Mixpanel governance agent.

Run with:  uvicorn server:app --reload --port 8000
Then open: http://localhost:8000

The agent core is unchanged — this drives the same ClaudeSDKClient that
main.py drives, and main.py still works for terminal use.

Shape of a turn:

    POST /api/ask      starts the turn, returns an SSE stream
    (stream carries)   text / tool / approval_request / done events
    POST /api/approve  resolves a pending write approval

The agent turn runs as a background task that pushes every event onto a
queue; the SSE response just drains that queue. That keeps approval
requests and model output on one ordered channel.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
    create_sdk_mcp_server,
)
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# Importing workspace first creates `ws` and fails fast on bad config.
from workspace import PROJECT_ID, WORKSPACE_ID_RESOLVED, load_business_context
from memory import MEMORY_PATH, load_memory
from tools import ALL_TOOLS, WRITE_TOOL_NAMES

SERVER_NAME = "governance"
STATIC_DIR = Path(__file__).parent / "static"
PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.md"
DEFAULT_MODEL = "claude-sonnet-5"
MAX_TURNS = 25

# How long a pending write waits for the browser before giving up. Without
# this, a closed tab would park the turn forever.
APPROVAL_TIMEOUT_SECONDS = 300

BLOCKED_TOOLS = [
    "Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "NotebookEdit",
    "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite", "SlashCommand",
]


# --------------------------------------------------------------------------
# Prompt and options
# --------------------------------------------------------------------------


def build_system_prompt(business_context: str, memory: str) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    # str.replace, not str.format: business context is arbitrary markdown and
    # any literal { } in it would break .format().
    return (
        template.replace("{today}", date.today().isoformat())
        .replace("{project_id}", str(PROJECT_ID))
        .replace("{workspace_id}", str(WORKSPACE_ID_RESOLVED))
        .replace("{business_context}", business_context or "No business context configured.")
        .replace("{memory}", memory or "Nothing remembered yet.")
    )


# --------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------


@dataclass
class Session:
    """One browser's conversation: a client, an event queue, pending approvals."""

    client: ClaudeSDKClient | None = None
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    busy: bool = False


STATE: dict[str, Any] = {
    "session": Session(),
    "business_context": "",
    "context_stats": {},
    "memory": "",
    "memory_entries": 0,
}


async def confirm_writes(
    tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
) -> PermissionResultAllow | PermissionResultDeny:
    """Pause any Mixpanel write until the browser approves it."""
    short_name = tool_name.split("__")[-1]
    if short_name not in WRITE_TOOL_NAMES:
        return PermissionResultAllow()

    session: Session = STATE["session"]
    request_id = uuid.uuid4().hex
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    session.pending[request_id] = future

    await session.queue.put(
        {
            "type": "approval_request",
            "id": request_id,
            "tool": short_name,
            "event_name": tool_input.get("event_name"),
            "fields": {
                k: v for k, v in tool_input.items() if k != "event_name" and v is not None
            },
        }
    )

    try:
        approved = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        approved = False
        await session.queue.put({"type": "approval_timeout", "id": request_id})
    finally:
        session.pending.pop(request_id, None)

    if approved:
        return PermissionResultAllow()
    return PermissionResultDeny(
        message=(
            "The user declined this change. Do not retry it or attempt the same "
            "edit another way. Report that it was not applied and ask what they "
            "would like instead."
        )
    )


def build_options() -> ClaudeAgentOptions:
    server = create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=ALL_TOOLS)
    return ClaudeAgentOptions(
        system_prompt=build_system_prompt(STATE["business_context"], STATE["memory"]),
        mcp_servers={SERVER_NAME: server},
        allowed_tools=[f"mcp__{SERVER_NAME}__{t.name}" for t in ALL_TOOLS],
        disallowed_tools=BLOCKED_TOOLS,
        max_turns=MAX_TURNS,
        model=os.getenv("GOVERNANCE_MODEL", DEFAULT_MODEL),
        setting_sources=[],
        strict_mcp_config=True,
        can_use_tool=confirm_writes,
        # Force the direct Anthropic API, ignoring any Vertex/Bedrock routing
        # inherited from the shell (e.g. CLAUDE_CODE_USE_VERTEX=1).
        env={
            "CLAUDE_CODE_USE_VERTEX": "0",
            "CLAUDE_CODE_USE_BEDROCK": "0",
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
        },
    )


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    STATE["business_context"], STATE["context_stats"] = load_business_context()
    STATE["memory"], STATE["memory_entries"] = load_memory()

    session: Session = STATE["session"]
    session.client = ClaudeSDKClient(options=build_options())
    await session.client.connect()
    print(f"Governance agent ready — project {PROJECT_ID}, workspace {WORKSPACE_ID_RESOLVED}")
    print("Open http://localhost:8000")
    try:
        yield
    finally:
        if session.client is not None:
            await session.client.disconnect()


app = FastAPI(title="Mixpanel Governance Agent", lifespan=lifespan)


# --------------------------------------------------------------------------
# The turn
# --------------------------------------------------------------------------


async def run_turn(session: Session, question: str) -> None:
    """Drive one question and push every event onto the queue."""
    try:
        await session.client.query(question)
        async for message in session.client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        await session.queue.put({"type": "text", "text": block.text})
                    elif isinstance(block, ToolUseBlock):
                        await session.queue.put(
                            {"type": "tool", "name": block.name.split("__")[-1]}
                        )
            elif isinstance(message, ResultMessage):
                await session.queue.put(
                    {
                        "type": "result",
                        "is_error": message.is_error,
                        "turns": message.num_turns,
                        "cost_usd": message.total_cost_usd,
                        "detail": message.result if message.is_error else None,
                    }
                )
    except Exception as exc:  # surface rather than hang the stream
        await session.queue.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        session.busy = False
        await session.queue.put({"type": "done"})


class AskRequest(BaseModel):
    question: str


@app.post("/api/ask")
async def ask(req: AskRequest) -> StreamingResponse:
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question must not be empty")

    session: Session = STATE["session"]
    if session.busy:
        raise HTTPException(status_code=409, detail="A question is already in progress")
    session.busy = True

    # Drop anything left over from an abandoned stream.
    while not session.queue.empty():
        session.queue.get_nowait()

    task = asyncio.create_task(run_turn(session, question))

    async def stream():
        try:
            while True:
                event = await session.queue.get()
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "done":
                    break
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ApprovalRequest(BaseModel):
    id: str
    approved: bool


@app.post("/api/approve")
async def approve(req: ApprovalRequest) -> dict[str, Any]:
    session: Session = STATE["session"]
    future = session.pending.get(req.id)
    if future is None or future.done():
        raise HTTPException(status_code=404, detail="No pending approval with that id")
    future.set_result(req.approved)
    return {"ok": True, "approved": req.approved}


# --------------------------------------------------------------------------
# Side panels
# --------------------------------------------------------------------------


@app.get("/api/status")
async def status() -> dict[str, Any]:
    return {
        "project_id": PROJECT_ID,
        "workspace_id": WORKSPACE_ID_RESOLVED,
        "tools": [t.name for t in ALL_TOOLS],
        "write_tools": sorted(WRITE_TOOL_NAMES),
        "context_stats": STATE["context_stats"],
        "memory_entries": STATE["memory_entries"],
        "model": os.getenv("GOVERNANCE_MODEL", DEFAULT_MODEL),
    }


@app.get("/api/context")
async def context() -> dict[str, Any]:
    return {"markdown": STATE["business_context"], "stats": STATE["context_stats"]}


@app.get("/api/memory")
async def memory() -> dict[str, Any]:
    markdown, count = load_memory()
    return {"markdown": markdown, "entries": count, "file": MEMORY_PATH.name}


@app.post("/api/clear")
async def clear() -> dict[str, Any]:
    """Start a fresh conversation, reloading memory written this session."""
    session: Session = STATE["session"]
    if session.busy:
        raise HTTPException(status_code=409, detail="A question is in progress")
    if session.client is not None:
        await session.client.disconnect()
    STATE["memory"], STATE["memory_entries"] = load_memory()
    session.client = ClaudeSDKClient(options=build_options())
    await session.client.connect()
    return {"ok": True, "memory_entries": STATE["memory_entries"]}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
