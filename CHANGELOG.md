# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

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
