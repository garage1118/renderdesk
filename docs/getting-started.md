# Getting started

This guide shows you how to connect an MCP client to renderdesk and
publish your first artifact.

## Before you start

You need a renderdesk account. If you do not have one, ask your
renderdesk administrator to create one for you.

The examples below use `https://renderdesk.example.com` as a
placeholder — replace it with your own instance's URL wherever it
appears.

renderdesk supports two ways to connect a client:

- **A personal token.** Use this for command-line agents such as Claude
  Code and OpenCode. You create the token once in the dashboard and
  paste it into your client's configuration.
- **OAuth.** Use this for browser-based clients such as Claude on the
  web and ChatGPT. You add the connector URL and the client walks you
  through login and approval.

Both methods give the client the same set of tools. Neither method
gives the client any dashboard-only capability, such as deleting an
artifact or sharing one with another user.

## Step 1: create a personal token

Skip this step if you plan to connect with OAuth instead.

1. Log in to the renderdesk dashboard.
2. Open **Connections**.
3. In the **Label** field, enter a name for this client, such as
   `laptop` or `opencode`.
4. Select **Create token**.
5. Copy the token. renderdesk shows it only once.

A personal token expires 90 days after you create it. When it expires,
create a new one and update your client's configuration.

## Step 2: connect your client

### Claude Code

Run this command, with your token in place of `YOUR_TOKEN`:

```
claude mcp add --transport http renderdesk https://renderdesk.example.com/mcp --header "Authorization: Bearer YOUR_TOKEN"
```

### OpenCode

For a personal token, run:

```
opencode mcp add renderdesk --url https://renderdesk.example.com/mcp --header "Authorization=Bearer YOUR_TOKEN"
```

For OAuth instead, add the server without a token, then authenticate:

```
opencode mcp add renderdesk --url https://renderdesk.example.com/mcp
opencode mcp auth renderdesk
```

The second command opens your browser to a login and approval page.

### Claude on the web, ChatGPT, and other browser-based clients

1. In your client, add a new connector or custom MCP server.
2. Enter this URL: `https://renderdesk.example.com/mcp`.
3. Follow the prompts to log in to renderdesk and approve the
   connection.

renderdesk issues an access token that expires after one hour and
refreshes automatically while your client stays connected.

## Step 3: publish your first artifact

Ask your agent to publish an artifact. For example:

> Publish an HTML page that says "Hello, renderdesk" as an artifact.

The agent calls the `publish_artifact` tool and returns a URL such as
`https://renderdesk.example.com/a/<id>`. Open the URL to view the
artifact.

## What counts as one connection

Each token or OAuth login creates one **connection**. A connection owns
only the artifacts it publishes. If you connect the same client again
with a new token, it starts as an empty connection and cannot see
artifacts from your other connections. Your dashboard login, by
contrast, shows every artifact from every connection you own.

## Limits

Every connection has these default limits:

- 200 artifacts per connection.
- 2,000,000 bytes (about 2 MB) per artifact.
- 50,000,000 bytes (about 50 MB) total per connection.

If a client hits a limit, `publish_artifact` or `update_artifact`
returns a `quota_exceeded` error. Delete an old artifact from the
dashboard to free up room, or ask your administrator to raise the
limit.

## Next steps

- Read the [MCP tool reference](mcp-tools.md) for every tool renderdesk
  exposes.
- Read the [dashboard guide](dashboard-guide.md) to browse, comment on,
  and share the artifacts your agent publishes.
