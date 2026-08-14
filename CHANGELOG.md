# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

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
