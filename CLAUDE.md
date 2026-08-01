# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

See `DESIGN_NOTES.md` for architecture/design rationale behind most
non-obvious decisions in this codebase — read it before making
non-trivial changes. `CLAUDE.local.md` (gitignored, local-only) has
environment/deploy/git behavioral rules specific to the maintainer's
setup, not design.

## What this is

renderdesk is a self-hosted store for LLM-generated artifacts (self-contained
HTML/Markdown/code/CSV documents), reachable over MCP so agents (Claude Code,
Claude on the web, ChatGPT, or any MCP client) can publish/update/list them
the way Claude's own `Artifact` tool does — except self-hosted. A web
dashboard lets a human browse, comment on, and share what gets published.

## Commands

```bash
uv run pytest                    # full test suite
uv run pytest tests/test_tools.py                       # one file
uv run pytest tests/test_tools.py::test_publish_artifact # one test
uv run ruff check .              # lint (config in pyproject.toml)
uv run uvicorn renderdesk.app:app --reload   # run dev server locally
uv run alembic revision --autogenerate -m "..."  # new migration
uv run alembic upgrade head      # apply migrations (also runs automatically at app startup)
```

No type checker is wired in yet (see `pyproject.toml`/CLAUDE.local.md for why).

Tests spin up a temporary SQLite DB per run (`tests/conftest.py`) and reset
all tables between tests via an autouse fixture — no separate test DB setup
needed. `RENDERDESK_PUBLIC_BASE_URL`/`RENDERDESK_AUTH_SCHEME` env vars are
defaulted there too, so `Settings` construction at import time doesn't fail.

## Architecture

**Stack:** FastAPI + SQLAlchemy (async, `aiosqlite`) + SQLite, Alembic
migrations, the official `mcp` Python SDK (`FastMCP`) for the MCP transport,
`authlib` for OIDC/JWT. Single-process, single-worker only today (see
"Known single-process assumptions" below) — this is load-bearing, not an
oversight.

**Two parallel surfaces, two parallel auth systems, deliberately never
crossing:**
- **MCP surface** (`/mcp`, `mcp_server.py`) — bearer-token authenticated
  (`auth.py`'s `MCPAuthMiddleware`), scoped to a `Connection` (a personal
  token or an OAuth-authorized client — see `models.py`). Every MCP tool
  reads the caller's `connection_id` from a contextvar
  (`get_current_connection_id()`) and every query is scoped to it — one
  connection can never see another connection's artifacts, even under the
  same user account. Tool implementations live in `tools.py`/`comments.py`/
  `shares.py`; `mcp_server.py` itself is just thin `@mcp.tool()` wrappers.
- **Dashboard surface** (`/dashboard`, `dashboard.py`) — session-cookie
  authenticated (`session_auth.py`), scoped to a `User`. Handles things
  deliberately kept off the MCP tool surface entirely: delete artifact,
  reassign an artifact to a different connection, share/unshare, connection
  management (create/revoke/delete tokens). These are dashboard-only by
  design — see "Deliberately withheld capabilities" in `DESIGN_NOTES.md` —
  not a gap to fill with new MCP tools.
- A third, smaller surface: renderdesk is its own OAuth 2.0 Authorization
  Server (`oauth_provider.py`, routes from the `mcp` SDK mounted on the
  top-level app, not nested under `/mcp`) so browser-based MCP clients can
  connect via PKCE instead of needing CLI access to mint a token. An
  OAuth-authorized client is still just a `Connection`, same as a
  CLI-minted one — `MCPAuthMiddleware` tries a bearer token as a personal
  token first, then as an OAuth access token; both converge on the same
  `connection_id` contextvar, so tool code never needs to know which kind
  authenticated the request.

**Data model** (`models.py`): `User` → `Connection` (personal token or OAuth
client) → `Artifact` → `ArtifactVersion` (append-only history, one full copy
per write, never diffs). `Comment` is self-referential (thread root has
`parent_id=None`, resolved state lives on the root); every comment has
exactly one real author (`author_user_id` xor `author_connection_id`), never
an anonymous "human"/"agent" tag. `ArtifactShare` is outbound-only from MCP
(no `get_artifact`-for-things-shared-with-me tool) — sharing only grants
dashboard visibility, never MCP-level access. OAuth state lives in
`OAuthClient`/`OAuthAuthorizationCode`/`OAuthAccessToken`/`OAuthRefreshToken`.
`AppSetting` is a generic key/value store for process-wide config that must
survive restarts (currently just the locked-in auth scheme).

**Auth schemes are pluggable** (`auth_scheme.py`): `RENDERDESK_AUTH_SCHEME`
(`password`/`oidc`/`saml` — only the first two are implemented) is locked in
via `AppSetting` on first boot and checked against on every later boot; a
mismatch fails startup rather than silently changing auth behavior.
Switching on purpose requires `renderdesk set-auth-scheme <scheme>` run
against the deployed container first. OIDC (`oidc.py`, `oidc_state.py`) uses
one generic issuer/client-id/secret config, covering any standards-compliant
IdP (Entra, Google Workspace, Okta, Authentik, Keycloak, ...) rather than
per-provider code.

**Rendering & CSP** (`view.py`, serves `/a/{id}` and `/a/{id}/raw`):
`markdown`/`code`/`csv` are rendered server-side into a fixed HTML skeleton
(sanitized via `nh3` for markdown; Pygments-escaped or `html.escape`d
otherwise) — never executes artifact-supplied script, gets the strictest
CSP. `html` artifacts are served byte-for-byte into a sandboxed iframe with
a CSP blocking network access (`connect-src 'none'`) — the enforced
constraint on publishers is that an HTML artifact must be fully
self-contained. Every asset (Mermaid, KaTeX, Pygments' CSS, fonts) is
vendored under `static/`, same-origin — zero external network calls from
any served page, a hard project-wide rule, not a preference (see the
`DESIGN_NOTES.md` fonts/CDN incident for what happens when this slips).
CSP is loosened (`script-src 'self'`, same-origin only) only on the specific
responses that actually need it (a diagram/CSV column-resize present),
never globally.

**Quotas & concurrency**: per-connection artifact-count/byte caps enforced
in `quotas.py` before every write, guarded by a per-connection
`asyncio.Lock` (`quota_lock`) to close a TOCTOU race — correct only because
this is a single-process deployment (see below).

**CLI** (`cli.py`, `renderdesk` entrypoint): the only way to provision the
first user/token (`create-user`/`create-token` — no self-serve signup;
later tokens can be self-served from `/dashboard/connections`) and the only
way to force-delete an artifact/connection or change the auth scheme
bypassing normal ownership scoping. Every destructive admin command
(`delete-artifact`, `revoke-token`, `delete-token`) requires the caller to
pass the exact current title/label + owner email as a non-interactive
correctness check (not a y/N prompt) — these commands are meant to be run
non-interactively, often by an agent with container shell access, not a
human at a terminal.

## Known single-process assumptions

Several things would actively break (not just fail to help) under
`uvicorn --workers > 1`, and are unaffected by a future Postgres migration
since they're process-local Python state, not a database limitation:
`oidc_state`/`oauth_consent_state`'s HMAC signing keys (random per process),
`session_auth`'s login-lockout tracker and `rate_limit`'s per-IP attempt
counters (in-memory dicts), and `quotas.quota_lock`/
`oauth_provider._connection_creation_locks` (`asyncio.Lock`s only serialize
within one process's event loop). See `DESIGN_NOTES.md` Roadmap items 3–4
before touching any of these or proposing multi-worker/Postgres support.

## Conventions worth preserving

- MCP tool docstrings that touch untrusted user-authored text (comments)
  explicitly warn against treating that content as instructions to follow —
  keep that framing in any new tool that surfaces user-generated text to a
  model.
- A non-owner (including someone an artifact is merely shared with) gets a
  404 from dashboard routes it can't act on, never a 403 — don't leak
  existence.
- Migrations that only add nullable columns/tables need no special rollout
  handling; anything that changes an existing constraint or backfills data
  needs a rollout note (see `DESIGN_NOTES.md` for past examples) since this
  project has exactly one live deployment to coordinate with.
- One commit per distinct fix/issue when doing a multi-issue pass (review
  triage, test fixes), not one combined commit — matches existing history.
