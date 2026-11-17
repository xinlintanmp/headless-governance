"""Web server for the Mixpanel governance agent.

Local:   uvicorn server:app --reload --port 8000
Hosted:  uvicorn server:app --host 0.0.0.0 --port $PORT

Each logged-in browser gets its own ClaudeSDKClient (and so its own
conversation). Mixpanel writes pause until that browser approves them.

Shape of a turn:

    POST /api/ask      starts the turn, returns an SSE stream
    (stream carries)   text / tool / approval_request / done events
    POST /api/approve  resolves a pending write approval
"""

from __future__ import annotations

import asyncio
import json
import os
import time
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
from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import auth
from workspace import PROJECT_ID, WORKSPACE_ID_RESOLVED, load_business_context
from tools import ALL_TOOLS, WRITE_TOOL_NAMES

SERVER_NAME = "governance"
STATIC_DIR = Path(__file__).parent / "static"
PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.md"
DEFAULT_MODEL = "claude-sonnet-5"
MAX_TURNS = 25

APPROVAL_TIMEOUT_SECONDS = 300
SESSION_IDLE_SECONDS = 60 * 60          # evict a browser's agent after an hour idle
REAPER_INTERVAL_SECONDS = 300
MAX_CONCURRENT_SESSIONS = int(os.getenv("MAX_SESSIONS", "10"))

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
# Per-browser sessions
# --------------------------------------------------------------------------


@dataclass
class Session:
    """One browser's conversation."""

    client: ClaudeSDKClient | None = None
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    busy: bool = False
    last_used: float = field(default_factory=time.monotonic)


SESSIONS: dict[str, Session] = {}

# Startup-time context, shared by every session.
CONTEXT: dict[str, Any] = {"business_context": "", "stats": {}, "memory": "", "entries": 0}

# Which session the currently-running turn belongs to. Set around each turn so
# the permission callback, which the SDK calls without session context, routes
# its approval request to the right browser.
CURRENT_SESSION: dict[str, Session] = {}


def load_team_memory() -> tuple[str, int]:
    """Read the agent-memory block from Mixpanel Business Context."""
    try:
        from team_memory import read_memory

        return read_memory()
    except Exception as exc:  # never block startup on memory
        print(f"  ! Could not read team memory: {exc}")
        return "", 0


async def confirm_writes(
    tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
) -> PermissionResultAllow | PermissionResultDeny:
    """Pause any Mixpanel write until the owning browser approves it."""
    short_name = tool_name.split("__")[-1]
    if short_name not in WRITE_TOOL_NAMES:
        return PermissionResultAllow()

    session = CURRENT_SESSION.get("session")
    if session is None:  # no browser to ask: refuse rather than write blind
        return PermissionResultDeny(message="No interactive session to approve this write.")

    request_id = uuid.uuid4().hex
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    session.pending[request_id] = future

    await session.queue.put(
        {
            "type": "approval_request",
            "id": request_id,
            "tool": short_name,
            "event_name": tool_input.get("event_name") or tool_input.get("note"),
            "fields": {
                k: v
                for k, v in tool_input.items()
                if k not in ("event_name", "note") and v is not None
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
        system_prompt=build_system_prompt(CONTEXT["business_context"], CONTEXT["memory"]),
        mcp_servers={SERVER_NAME: server},
        allowed_tools=[f"mcp__{SERVER_NAME}__{t.name}" for t in ALL_TOOLS],
        disallowed_tools=BLOCKED_TOOLS,
        max_turns=MAX_TURNS,
        model=os.getenv("GOVERNANCE_MODEL", DEFAULT_MODEL),
        setting_sources=[],
        strict_mcp_config=True,
        can_use_tool=confirm_writes,
        # Force the direct Anthropic API, ignoring any Vertex/Bedrock routing
        # inherited from the environment (e.g. CLAUDE_CODE_USE_VERTEX=1).
        env={
            "CLAUDE_CODE_USE_VERTEX": "0",
            "CLAUDE_CODE_USE_BEDROCK": "0",
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
        },
    )


async def get_session(session_id: str) -> Session:
    """Return this browser's session, creating its agent on first use."""
    session = SESSIONS.get(session_id)
    if session is None:
        if len(SESSIONS) >= MAX_CONCURRENT_SESSIONS:
            await reap_idle(force=True)
        if len(SESSIONS) >= MAX_CONCURRENT_SESSIONS:
            raise HTTPException(
                status_code=503,
                detail="Too many active sessions. Try again in a few minutes.",
            )
        session = Session()
        SESSIONS[session_id] = session
    if session.client is None:
        session.client = ClaudeSDKClient(options=build_options())
        await session.client.connect()
    session.last_used = time.monotonic()
    return session


async def close_session(session_id: str) -> None:
    session = SESSIONS.pop(session_id, None)
    if session and session.client is not None:
        try:
            await session.client.disconnect()
        except Exception:
            pass


async def reap_idle(force: bool = False) -> None:
    """Disconnect agents for browsers that have gone quiet."""
    now = time.monotonic()
    for session_id, session in list(SESSIONS.items()):
        if session.busy:
            continue
        idle = now - session.last_used
        if idle > SESSION_IDLE_SECONDS or (force and idle > 60):
            await close_session(session_id)


async def reaper() -> None:
    while True:
        await asyncio.sleep(REAPER_INTERVAL_SECONDS)
        try:
            await reap_idle()
        except Exception as exc:
            print(f"  ! session reaper: {exc}")


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    CONTEXT["business_context"], CONTEXT["stats"] = load_business_context()
    CONTEXT["memory"], CONTEXT["entries"] = load_team_memory()

    print(f"Governance agent ready — project {PROJECT_ID}, workspace {WORKSPACE_ID_RESOLVED}")
    print(f"  business context: {sum(CONTEXT['stats'].values()):,} chars")
    print(f"  team memory: {CONTEXT['entries']} entries")
    print(f"  auth: {'password required' if auth.auth_required() else 'OPEN (no APP_PASSWORD set)'}")

    task = asyncio.create_task(reaper())
    try:
        yield
    finally:
        task.cancel()
        for session_id in list(SESSIONS):
            await close_session(session_id)


app = FastAPI(title="Mixpanel Governance Agent", lifespan=lifespan)


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


@app.middleware("http")
async def require_login(request: Request, call_next):
    if not auth.auth_required() or request.url.path in auth.PUBLIC_PATHS:
        return await call_next(request)
    if auth.verify_token(request.cookies.get(auth.COOKIE_NAME)):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return FileResponse(STATIC_DIR / "login.html", status_code=401)


class LoginRequest(BaseModel):
    password: str


@app.post("/api/login")
async def login(req: LoginRequest, response: Response) -> dict[str, Any]:
    if not auth.check_password(req.password):
        raise HTTPException(status_code=401, detail="Incorrect password")
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.issue_token(),
        httponly=True,
        samesite="lax",
        secure=os.getenv("COOKIE_SECURE", "1") == "1",
        max_age=auth.SESSION_TTL_SECONDS,
    )
    return {"ok": True}


@app.get("/login")
async def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def session_id_from(cookie: str | None) -> str:
    """Session id from the signed cookie, or a fixed one when auth is off."""
    if not auth.auth_required():
        return "local"
    session_id = auth.verify_token(cookie)
    if session_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return session_id


# --------------------------------------------------------------------------
# The turn
# --------------------------------------------------------------------------


async def run_turn(session: Session, question: str) -> None:
    """Drive one question and push every event onto the session's queue."""
    CURRENT_SESSION["session"] = session
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
    except Exception as exc:
        await session.queue.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        session.busy = False
        session.last_used = time.monotonic()
        CURRENT_SESSION.pop("session", None)
        await session.queue.put({"type": "done"})


class AskRequest(BaseModel):
    question: str


@app.post("/api/ask")
async def ask(
    req: AskRequest, governance_session: str | None = Cookie(default=None)
) -> StreamingResponse:
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question must not be empty")

    session = await get_session(session_id_from(governance_session))
    if session.busy:
        raise HTTPException(status_code=409, detail="A question is already in progress")
    session.busy = True

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
async def approve(
    req: ApprovalRequest, governance_session: str | None = Cookie(default=None)
) -> dict[str, Any]:
    # Looked up on the caller's own session, so one browser cannot approve
    # another browser's pending write.
    session = SESSIONS.get(session_id_from(governance_session))
    future = session.pending.get(req.id) if session else None
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
        "context_stats": CONTEXT["stats"],
        "memory_entries": CONTEXT["entries"],
        "model": os.getenv("GOVERNANCE_MODEL", DEFAULT_MODEL),
        "active_sessions": len(SESSIONS),
    }


@app.get("/api/context")
async def context() -> dict[str, Any]:
    return {"markdown": CONTEXT["business_context"], "stats": CONTEXT["stats"]}


@app.get("/api/memory")
async def memory() -> dict[str, Any]:
    markdown, count = await asyncio.to_thread(load_team_memory)
    return {
        "markdown": markdown or "(nothing remembered yet)",
        "entries": count,
        "file": "Mixpanel project Business Context",
    }


@app.post("/api/clear")
async def clear(governance_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    """Start a fresh conversation, reloading memory written since startup."""
    session_id = session_id_from(governance_session)
    session = SESSIONS.get(session_id)
    if session and session.busy:
        raise HTTPException(status_code=409, detail="A question is in progress")
    await close_session(session_id)
    CONTEXT["memory"], CONTEXT["entries"] = await asyncio.to_thread(load_team_memory)
    return {"ok": True, "memory_entries": CONTEXT["entries"]}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
