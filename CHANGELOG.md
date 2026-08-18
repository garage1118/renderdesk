# Changelog

All notable changes to this project are documented in this file.

## [1.2.1] - 2026-08-17

### Security

This release addresses findings from an internal security review. Upgrading
is recommended for all deployments.

- MCP tool calls are now authorized against the requesting connection's own
  bearer token, not the connection that originally opened the session. The
  server previously ran FastMCP's stateful session manager, which let a
  holder of any valid token who learned another connection's live
  `Mcp-Session-Id` read, update, and share that connection's artifacts.
  Running the transport stateless also closes a related resource leak, where
  each session-creating request held a transport/task open for the life of
  the process.
- Three regexes on the unauthenticated artifact-rendering path (`GET
  /a/{id}/raw`) could be driven into catastrophic backtracking by
  attacker-controlled artifact content, pinning the single worker process at
  100% CPU. Replaced with linear scans.
- Rendering a CSV artifact as an HTML table had no row cap, so a
  multi-million-row file could expand into hundreds of MB of transient
  allocation per request. Rendering is now capped and offloaded off the
  event loop.
- Per-connection quotas only accounted for an artifact's current content
  size, not its version history or title/language metadata, letting a
  connection under quota write far more to disk than its limit implied via
  repeated updates or an oversized title. Quotas now account for stored
  version history, and old versions are pruned on write.
- Comments had no quota, no rate limit, and no way to delete an individual
  thread — a share recipient (who never owns the artifact) could write
  unbounded comment rows against someone else's artifact with no way for
  the owner to reclaim the space short of deleting the whole artifact.
  Comments are now quota-capped per artifact, rate-limited, paginated on
  the dashboard, and individually deletable by the artifact owner.
- The Pygments syntax highlighter reused one long-lived formatter instance
  across every request; a caching detail in the pinned Pygments version
  meant that instance retained highlighted attacker-supplied text for the
  life of the process. Each render now gets its own formatter.
- Fixed an open redirect in the post-login `next` parameter: a value like
  `///evil.com` bypassed the existing same-site check due to how browsers
  resolve multiple leading slashes.
- The dashboard login lockout was keyed on the submitted email alone and
  checked before the password, so an unauthenticated caller who knew a
  user's email could lock that user out of their own account indefinitely.
  Credentials are now checked first; the real owner can always log in.
- Login response time distinguished existing accounts from nonexistent ones
  (a real bcrypt check only ran for existing accounts), letting an
  unauthenticated caller enumerate registered emails by timing. A dummy
  hash is now checked in both cases.
- Per-IP rate limiting silently collapsed into a single shared bucket when
  `RENDERDESK_TRUSTED_PROXY_IPS` was unset behind a reverse proxy (the
  shipped default), letting a handful of requests lock out every real user.
  The app now warns loudly at startup when this looks misconfigured, and
  rejects the wildcard (`*`) value outright. Successful logins also no
  longer count against the same budget as failed ones.
- Dynamic OAuth client registration (`/register`) accepted unbounded
  request bodies and client metadata with no size limit, letting an
  unauthenticated caller fill the disk or OOM the process. Both `/register`
  and `/mcp` now enforce request-body size limits, and registered client
  metadata is capped.
- The OIDC login callback reflected the identity provider's `error`/
  `error_description` query parameters directly into the login page with no
  authentication required to reach that code path, letting an attacker
  place arbitrary text on the real login page. Errors are now mapped to a
  fixed, allowlisted set of messages.
- The OIDC login state cookie could be replayed to fund unlimited outbound
  token-exchange requests against the identity provider, and neither the
  OIDC login/callback routes nor the state cookie's lifetime were bounded
  server-side. The state is now single-use and time-bounded, and both
  routes are rate-limited.
- `email_verified` in OIDC claims was coerced with `bool()`, so an identity
  provider emitting the non-standard string `"false"` was treated as
  verified — letting an attacker at a permissive IdP pre-register an
  account keyed to someone else's unverified email. A strict boolean check
  is now required.
- A background sweep that prunes expired OAuth rows could hit a foreign-key
  violation under a specific timing window and silently die for the rest of
  the process's life, after which nothing pruned expired sessions or
  in-memory rate-limit state. The underlying query is fixed, and the sweep
  loop now survives a failing iteration.
- Migration `0010`'s schema change was not safely re-runnable: an upgrade
  interrupted partway through left a host permanently unable to start on
  any later boot. It's now idempotent.
- The documented `curl` recipe for uploading large artifacts placed a live
  MCP token directly in a shell command argument, readable by any other
  local user via `ps`/`/proc`. It's now passed via a private, auto-cleaned
  config file instead.
- The OAuth consent screen displayed a registrant-supplied "scope" as if it
  limited what a connection could do, though nothing has ever enforced
  scope — approving a client believed to be read-only in fact granted full
  access. The screen no longer implies a limit that doesn't exist, and
  registrable scopes are now constrained.
- A cookie meant to bind an OAuth consent approval to the browser that
  actually initiated it could be re-stamped by an unrelated redirect,
  weakening the guarantee it was meant to provide. It's now stamped only
  by the authorization endpoint itself.
- The GitHub Actions release workflow referenced third-party actions by
  mutable version tag rather than pinned commit, and declared no explicit
  permissions. Actions are now pinned to commit SHAs (with Dependabot
  tracking updates), and the workflow's `GITHUB_TOKEN` is scoped to
  read-only.

## [1.2.0] - 2026-08-15

### Added

- `react` artifacts can now import `three`, `lodash`, `d3`, `mathjs`,
  `chart.js`/`chart.js/auto`, `tone`, `papaparse`, and `xlsx` (SheetJS), each
  vendored from its official standalone/UMD build and loaded only when an
  artifact actually imports it. `html` artifacts get the same 8 libraries
  via a `cdnjs.cloudflare.com` URL rewrite: a `<script src>` referencing one
  of them on cdnjs is transparently swapped for the same-origin vendored
  copy, so a model reaching for the URL pattern it already knows just works.
- Bootstrap Icons (`bi bi-<name>` classes), vendored as CSS + webfont —
  usable in both `react` and `html` with no import at all.

### Changed

- **Breaking (MCP):** a comment's `author` is now the real identity behind
  it — the email of the person who wrote it, or the label of the agent
  connection that did — instead of the bare string `"human"`/`"agent"`. A
  new `author_kind` field carries that human-vs-agent distinction, since a
  connection label and an email address aren't distinguishable by shape.
  Clients keying off `author == "agent"` need to read `author_kind`
  instead. Both fields are user-authored text and, like a comment body,
  are not to be treated as instructions. The dashboard now shows who
  actually said what, rather than rendering every participant in a thread
  as "human" or "agent".
- Artifact version history records each version's `byte_size` instead of
  measuring it from content on every page render, and the dashboard's
  artifact list, version history, "Shared with you" section and the
  `list_artifacts` MCP tool no longer load artifact content they never
  display. Version history is not quota-capped, so the version-history
  page — the one you visit to prune it — previously read every superseded
  version's full body into memory just to show a list of sizes.
- Migrations `0001`-`0009` are collapsed into a single baseline. The
  baseline keeps the revision id `0009_oidc_identities`, which every
  published image ships as its head, so existing installations resolve it
  and simply have nothing to re-run — no manual `alembic stamp`, and
  upgrades stay unattended on hosts we can't reach.

- The hourly cleanup loop now also sweeps expired dashboard sessions and
  the in-memory rate-limit/login-lockout counters. Each was only ever
  pruned as a side effect of someone touching the same row or key again,
  which never happens for abandoned ones — and the counters are keyed by
  client IP and submitted email, both chosen by unauthenticated callers,
  so they grew without bound for the lifetime of the process.

### Fixed

- `update_artifact` rejected `format="react"`: the MCP tool's schema still
  listed only the four pre-React formats, so an artifact could be
  published as `react` but never converted to it, and a client restating
  the current format on update got an unresolvable validation error.
- Bootstrap Icons never rendered in `html` or `react` artifacts — every
  `bi-*` icon showed as a tofu box of its raw Private Use Area codepoint
  (e.g. `F589` for `bi-stars`). Artifacts are served from a sandboxed
  iframe with no `allow-same-origin`, giving them an opaque origin, and
  webfonts are the one subresource type always fetched in CORS mode — so
  the vendored woff2 was blocked while the stylesheet, which isn't
  CORS-gated, loaded and inserted the glyph placeholder. `/static` is now
  served with `Access-Control-Allow-Origin: *`; the sandbox is unchanged.
- Artifact responses now state `X-Frame-Options: SAMEORIGIN` to match the
  `frame-ancestors 'self'` in their CSP, instead of inheriting the
  dashboard's `DENY` default and contradicting it. No behaviour change —
  browsers ignore `X-Frame-Options` when `frame-ancestors` is present, so
  the same-origin iframe used to render `html`/`react` artifacts was
  already working, just on a spec fallback rather than on intent.

## [1.1.0] - 2026-08-14

### Added

- `WWW-Authenticate` header on unauthenticated `/mcp` responses, pointing
  clients at the protected-resource metadata URL (RFC 9728) so OAuth-capable
  clients can discover it without already knowing to look.
- Dashboard: rename any connection's label — previously only settable once,
  at creation. The Save button only appears once the label has actually
  changed.
- `upload_large_artifact` MCP prompt: a documented `curl` recipe for
  publishing artifacts too large to generate as tool-call output (e.g.
  self-contained HTML with embedded base64 images), for clients with local
  shell access.

### Changed

- Docker images: added an `:edge` tag that always points at the newest
  tagged release (RC or not), for tracking a testbed deployment; the
  release workflow can also be dispatched manually to build an edge-only
  image on demand.

## [1.0.0] - 2026-08-12

Initial public release: MCP artifact publishing/updating/listing, comments,
sharing, a web dashboard, password/OIDC auth, and OAuth 2.0 (PKCE) for
browser-based MCP clients.
