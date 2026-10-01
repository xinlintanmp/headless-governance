"""Terminal loop and Claude Agent SDK client for the Mixpanel governance agent.

Run with:  python main.py

Startup order matters: importing ``workspace`` constructs the single shared
``ws`` and validates credentials before anything else happens.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    CLINotFoundError,
    ProcessError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
)

# Importing workspace first creates `ws` and fails fast on bad config.
from workspace import (
    PROJECT_ID,
    WORKSPACE_ID_RESOLVED,
    load_business_context,
)
from tools import ALL_TOOLS

SERVER_NAME = "governance"
PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.md"
DEFAULT_MODEL = "claude-sonnet-5"
MAX_TURNS = 10

# Built-in tools Claude must not reach. The allowlist alone already excludes
# them; this is a second, explicit barrier so the agent cannot read files,
# run shell commands, or fetch the network to work around the tool list.
BLOCKED_TOOLS = [
    "Bash",
    "BashOutput",
    "KillShell",
    "Read",
    "Write",
    "Edit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
    "TodoWrite",
    "SlashCommand",
]

DIM = "\033[2m"
RESET = "\033[0m"


# --------------------------------------------------------------------------
# Startup helpers
# --------------------------------------------------------------------------


def build_system_prompt(business_context: str) -> str:
    """Read the prompt template and substitute the four placeholders."""
    try:
        template = PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Could not read {PROMPT_PATH}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    # str.replace, not str.format: the business context is arbitrary user
    # markdown and any literal { } in it would break .format().
    return (
        template.replace("{today}", date.today().isoformat())
        .replace("{project_id}", str(PROJECT_ID))
        .replace("{workspace_id}", str(WORKSPACE_ID_RESOLVED))
        .replace("{business_context}", business_context or "No business context configured.")
    )


def build_options(system_prompt: str) -> ClaudeAgentOptions:
    """Register the tools in an in-process MCP server and build SDK options."""
    server = create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=ALL_TOOLS)
    allowed = [f"mcp__{SERVER_NAME}__{t.name}" for t in ALL_TOOLS]

    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={SERVER_NAME: server},
        allowed_tools=allowed,
        disallowed_tools=BLOCKED_TOOLS,
        max_turns=MAX_TURNS,
        model=os.getenv("GOVERNANCE_MODEL", DEFAULT_MODEL),
        # Do not load CLAUDE.md, project settings, or user settings.
        setting_sources=[],
        strict_mcp_config=True,
        # Force direct Anthropic API, ignoring any Vertex/Bedrock routing
        # inherited from the shell (e.g. CLAUDE_CODE_USE_VERTEX=1).
        env={
            "CLAUDE_CODE_USE_VERTEX": "0",
            "CLAUDE_CODE_USE_BEDROCK": "0",
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
        },
    )


def format_context_summary(stats: dict[str, int]) -> str:
    org, project = stats.get("organization", 0), stats.get("project", 0)
    if not org and not project:
        return "Business context: none configured"
    parts = []
    if org:
        parts.append(f"org {org:,} chars")
    if project:
        parts.append(f"project {project:,} chars")
    return "Business context loaded: " + ", ".join(parts)


# --------------------------------------------------------------------------
# Response streaming
# --------------------------------------------------------------------------


async def stream_answer(client: ClaudeSDKClient) -> None:
    """Print Claude's text and tool activity for one question."""
    printed_any = False

    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text, end="", flush=True)
                    printed_any = True
                elif isinstance(block, ToolUseBlock):
                    name = block.name.split("__")[-1]
                    if printed_any:
                        print()
                        printed_any = False
                    print(f"{DIM}[calling {name}…]{RESET}", flush=True)

        elif isinstance(message, ResultMessage):
            if printed_any:
                print()
            if message.is_error:
                detail = message.result or message.stop_reason or "unknown error"
                print(f"{DIM}[run ended with an error: {detail}]{RESET}")
            cost = message.total_cost_usd
            if cost is not None:
                print(f"{DIM}[{message.num_turns} turns · ${cost:.4f}]{RESET}")


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

HELP = """Commands:
  /exit      quit
  /clear     start a fresh conversation
  /context   show the loaded business context summary
"""


async def run() -> None:
    print("Starting Mixpanel governance agent…")
    print(f"  project {PROJECT_ID} · workspace {WORKSPACE_ID_RESOLVED}")

    business_context, stats = load_business_context()
    print("  " + format_context_summary(stats))

    options = build_options(build_system_prompt(business_context))

    client = ClaudeSDKClient(options=options)
    try:
        await client.connect()
    except CLINotFoundError as exc:
        print(
            f"\nCould not start the Claude CLI: {exc}\n"
            "Install it with:  npm install -g @anthropic-ai/claude-code",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    except (CLIConnectionError, ProcessError) as exc:
        print(
            f"\nCould not connect to Claude: {exc}\n"
            "Check ANTHROPIC_API_KEY, or run `claude login`.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    print(f"\nReady. {len(ALL_TOOLS)} governance tools, read-only. /exit to quit.\n")

    try:
        while True:
            try:
                question = input("› ").strip()
            except EOFError:
                print()
                break

            if not question:
                continue

            command = question.lower()
            if command in ("/exit", "/quit"):
                break
            if command == "/help":
                print(HELP)
                continue
            if command == "/context":
                print(format_context_summary(stats))
                if business_context:
                    print(business_context)
                continue
            if command == "/clear":
                await client.disconnect()
                client = ClaudeSDKClient(options=options)
                await client.connect()
                print("Conversation cleared.\n")
                continue

            try:
                await client.query(question)
                await stream_answer(client)
            except (CLIConnectionError, ProcessError) as exc:
                print(f"\n{DIM}[connection lost: {exc}]{RESET}")
                break
            print()
    finally:
        await client.disconnect()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nBye.")
    else:
        print("Bye.")


if __name__ == "__main__":
    main()
