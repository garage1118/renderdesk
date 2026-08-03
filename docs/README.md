# renderdesk documentation

renderdesk is a self-hosted store for artifacts that an AI agent
generates. An agent publishes an HTML, Markdown, code, or CSV document
through the Model Context Protocol (MCP). renderdesk stores it and gives
back a shareable link. A web dashboard lets a human browse, comment on,
and share these artifacts.

## Guides

- [Getting started](getting-started.md): connect an MCP client (Claude
  Code, Claude on the web, ChatGPT, or another MCP client) and publish
  your first artifact.
- [Dashboard guide](dashboard-guide.md): log in to the web dashboard and
  use its features, from comments to version history.
- [MCP tool reference](mcp-tools.md): every MCP tool renderdesk exposes,
  with parameters, return values, and error codes.
- [CLI reference](cli-reference.md): commands for administrators with
  shell access to the deployed container — provisioning users, force
  deleting artifacts, linking SSO identities, and changing the auth
  scheme.

## Who these guides are for

The first three guides assume you already have a renderdesk account. If
you do not have one, ask your renderdesk administrator to create one for
you. The CLI reference is written for administrators instead — it
assumes shell access to the deployed container, not a dashboard account.
