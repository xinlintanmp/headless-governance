# Business Context

Read and write the markdown documentation that grounds AI assistants in your organization's structure and goals, exposed as a Python API and `mp business-context` CLI group.

What is Business Context?

Business Context is plain markdown text (up to **50,000 characters per scope**) that you attach to a Mixpanel organization or project. AI assistants read it before answering questions, so they know *what your product does*, *what your events mean*, *which dashboards are canonical*, and *how your team defines key metrics*. See the [official Mixpanel Business Context docs](https://docs.mixpanel.com/docs/business-context) for the broader product picture.

Prerequisites

Business Context requires **authentication**. Project-level reads work with any account that has project access; project-level writes additionally require `edit_project_info` permission. Org-level operations require org membership (read) plus `edit_project_info` at the org level (write). Service accounts can read/write at the project level for projects they're attached to; for org-level operations or for org-id auto-resolution, an OAuth account (`oauth_browser` or `oauth_token`) is the cleanest path.

## Two scopes

| Scope          | Lives at                  | Shared by                |
| -------------- | ------------------------- | ------------------------ |
| `organization` | The Mixpanel organization | Every project in the org |
| `project`      | A single project          | That project only        |

`mixpanel_headless` exposes both scopes through the same API, gated by a `level: Literal["organization", "project"]` argument.

## Quick reference

```
import mixpanel_headless as mp

ws = mp.Workspace()

# Read
project_ctx = ws.get_business_context(level="project")
org_ctx = ws.get_business_context(level="organization")

# Read both at once (single round-trip via /business-context/chain)
chain = ws.get_business_context_chain()

# Write
ws.set_business_context("# About Acme\n…", level="project")
ws.set_business_context("# Org-wide context", level="organization")

# Clear (equivalent to set_business_context(""))
ws.clear_business_context(level="project")
```

```
# Read
mp business-context get --level project
mp business-context get --level organization
mp business-context chain                       # both at once

# Write — three input modes
mp business-context set --level project --content "# About Acme..."
mp business-context set --level project --file context.md
cat context.md | mp business-context set --level project

# Clear
mp business-context clear --level project
```

## Reading context

### Project scope

Project-scope reads use the active session's project ID. If no context has been set, the API returns the empty string — no special "not found" error to handle.

```
ctx = ws.get_business_context(level="project")
print(f"{ctx.character_count} chars")
if ctx.is_empty:
    print("No project context configured.")
else:
    print(ctx.content)
```

```
# Pretty-printed JSON (default)
mp business-context get --level project

# Just the markdown body via jq
mp business-context get --level project --jq '.content'

# Compact rich table
mp business-context get --level project --format table
```

### Organization scope

Organization-scope reads default to the org that owns the active session's project. The org ID is auto-resolved from the cached `/me` response (24-hour TTL). To read context from a different org without switching projects, pass `organization_id` explicitly.

```
# Auto-resolve org_id from the active project's organization
org_ctx = ws.get_business_context(level="organization")
print(f"org={org_ctx.organization_id}: {org_ctx.character_count} chars")

# Explicit override (skips the /me lookup)
other = ws.get_business_context(level="organization", organization_id=42)
```

```
# Auto-resolve from active project
mp business-context get --level organization

# Explicit org id
mp business-context get --level organization --organization-id 42
```

If auto-resolution can't determine the org (e.g. the active project isn't in the cached `/me` and the user belongs to multiple orgs), the call raises `WorkspaceScopeError` with `code="ORGANIZATION_AMBIGUOUS"` and lists the accessible org IDs.

### Both scopes in one call

The server exposes a `/business-context/chain` endpoint that returns both org and project context together, scoped to the active project. Use `get_business_context_chain()` (Python) or `mp business-context chain` (CLI) to avoid two round-trips.

`organization.organization_id` on the returned chain is populated **best-effort** from the cached `/me` response (in-memory or disk). When the cache is cold the field is left as `None` — the chain endpoint deliberately does *not* trigger an extra `/me` fetch, preserving its single-network-round-trip property. Callers that need a guaranteed org ID should call `get_business_context(level="organization")`, which performs full resolution.

```
chain = ws.get_business_context_chain()
print("ORG:    ", chain.organization.content)
print("PROJECT:", chain.project.content)
```

```
mp business-context chain
mp business-context chain --jq '.project.content'
```

## Writing context

`set_business_context` is **full-replace** semantics — what you pass becomes the entire stored content for that scope. There is no append, no patch, no diff. Pass the empty string to clear, or use `clear_business_context` for clarity.

```
new_content = """# Acme Analytics

## Product overview

Acme is a SaaS dashboard for SMBs.

## Event taxonomy

- `signup_completed` — user creates an account
- `subscription_started` — paid plan begins
- `feature_X_used` — pattern for feature engagement

## Definitions

- **Active user**: any user with ≥1 event in the last 28 days
"""

ws.set_business_context(new_content, level="project")
ws.set_business_context("# Org-wide standards…", level="organization")
ws.set_business_context("", level="project")  # clear
ws.clear_business_context(level="project")    # same thing, more explicit
```

The `set` command accepts content from three sources, in priority order:

1. `--content TEXT` — inline markdown (best for short content; pass `""` to clear)
1. `--file PATH` — read from a file on disk
1. **stdin** — when no flags are given and stdin is not a TTY

`--content` and `--file` are mutually exclusive. Stdin is only consulted when neither flag is provided. **Empty / whitespace-only stdin is rejected** (exit code 3) — use `mp business-context clear` to deliberately clear, so a CI/cron run with \`

Copy markdown
