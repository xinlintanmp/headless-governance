"""Single shared mixpanel_headless Workspace plus Business Context loading.

This module is the only place a ``Workspace`` is constructed. Importing it
loads ``.env``, validates configuration, and builds ``ws`` once. Every other
module imports ``ws`` from here rather than creating its own.

Import direction is one-way: main.py -> tools.py -> workspace.py
"""

from __future__ import annotations

import os
import sys

import mixpanel_headless as mp
from dotenv import load_dotenv
from mixpanel_headless import (
    AuthenticationError,
    ConfigError,
    MixpanelHeadlessError,
)

# --------------------------------------------------------------------------
# 1. Environment
# --------------------------------------------------------------------------

load_dotenv()


def _fail(message: str) -> "None":
    """Print a human-readable startup failure and exit non-zero."""
    print(f"\nConfiguration error\n-------------------\n{message}\n", file=sys.stderr)
    raise SystemExit(1)


# All governance operations are workspace-scoped, so this is required.
WORKSPACE_ID = os.getenv("MP_WORKSPACE_ID")
if not WORKSPACE_ID:
    _fail(
        "MP_WORKSPACE_ID is not set.\n"
        "Every Lexicon / governance operation is scoped to a workspace.\n"
        "Copy .env.example to .env and set MP_WORKSPACE_ID to your numeric\n"
        "workspace ID (find it in the Mixpanel URL, or run: mp workspace list)."
    )

# --------------------------------------------------------------------------
# 2. The single shared Workspace
# --------------------------------------------------------------------------

try:
    ws = mp.Workspace()
except ConfigError as exc:
    _fail(
        f"Mixpanel credentials are missing or unreadable: {exc}\n\n"
        "Set MP_PROJECT_ID plus either:\n"
        "  - MP_USERNAME and MP_SECRET (service account), or\n"
        "  - MP_OAUTH_TOKEN\n"
        "in your .env file. See .env.example."
    )
except AuthenticationError as exc:
    _fail(
        f"Mixpanel rejected the credentials (401): {exc}\n"
        "Check that the service account is still active and has access to\n"
        "this project and workspace."
    )
except MixpanelHeadlessError as exc:  # any other library-level startup failure
    _fail(f"Could not initialise the Mixpanel workspace: {exc}")


# --------------------------------------------------------------------------
# 3. Identifiers exposed for the system prompt
# --------------------------------------------------------------------------


def _safe_project_id() -> str:
    """Resolved project ID, or a placeholder if it cannot be resolved."""
    try:
        project = ws.project
    except MixpanelHeadlessError:
        return "unknown"
    return str(getattr(project, "id", "unknown"))


def _safe_workspace_id() -> str:
    """Resolved workspace ID, falling back to the configured value."""
    try:
        workspace = ws.workspace
    except MixpanelHeadlessError:
        return str(WORKSPACE_ID)
    if workspace is None:  # lazy session: not resolved yet
        return str(WORKSPACE_ID)
    return str(getattr(workspace, "id", WORKSPACE_ID))


PROJECT_ID = _safe_project_id()
WORKSPACE_ID_RESOLVED = _safe_workspace_id()


# --------------------------------------------------------------------------
# 4. Business Context
# --------------------------------------------------------------------------


def _section(title: str, context: object) -> str:
    """Render one business-context scope, or '' when it is empty."""
    if context is None or getattr(context, "is_empty", True):
        return ""
    content = (getattr(context, "content", "") or "").strip()
    if not content:
        return ""
    return f"### {title}\n\n{content}"


def load_business_context() -> tuple[str, dict[str, int]]:
    """Load org + project Business Context as one markdown string.

    Returns:
        ``(markdown, stats)`` where ``markdown`` has an "Organization context"
        and a "Project context" section (empty scopes omitted, ``""`` when both
        are empty), and ``stats`` maps ``"organization"`` / ``"project"`` to
        their character counts. Never raises: on failure it degrades to a
        project-only read, then to an empty string with a printed warning.
    """
    stats: dict[str, int] = {"organization": 0, "project": 0}
    sections: list[str] = []

    # Preferred path: one request returning both scopes.
    try:
        chain = ws.get_business_context_chain()
    except MixpanelHeadlessError as exc:
        # Organization scope is not readable by every service account, so
        # fall back to the project scope on its own.
        print(f"  ! Could not read the business context chain: {exc}")
        try:
            project_ctx = ws.get_business_context(level="project")
        except MixpanelHeadlessError as exc2:
            print(f"  ! Could not read project business context either: {exc2}")
            print("  ! Continuing without business context.")
            return "", stats
        stats["project"] = getattr(project_ctx, "character_count", 0) or 0
        section = _section("Project context", project_ctx)
        return (section, stats) if section else ("", stats)

    org_ctx = getattr(chain, "organization", None)
    project_ctx = getattr(chain, "project", None)

    if org_ctx is not None:
        stats["organization"] = getattr(org_ctx, "character_count", 0) or 0
    if project_ctx is not None:
        stats["project"] = getattr(project_ctx, "character_count", 0) or 0

    for title, ctx in (("Organization context", org_ctx), ("Project context", project_ctx)):
        section = _section(title, ctx)
        if section:
            sections.append(section)

    return ("\n\n".join(sections), stats)
