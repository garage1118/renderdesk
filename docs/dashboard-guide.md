# Dashboard guide

The renderdesk dashboard is the web interface for the artifacts your
agents publish. Use it to browse artifacts, comment on them, share
them, and manage the connections that publish them.

## Log in

Go to your renderdesk instance and open the login page. Depending on
how your administrator set up renderdesk, you sign in with an email and
password, or with your organization's single sign-on provider.

If you do not have an account, ask your renderdesk administrator to
create one for you.

## Your artifacts

The **Artifacts** page lists every artifact you own, across every
connection. Each row shows the title, format, owning connection, and
last update time. Each row has two links:

- **Open** takes you to the artifact's dashboard page, with comments
  and sharing controls.
- **View** opens the artifact itself in a new tab, the same page an
  agent gets back from `publish_artifact`.

Select **Delete** to remove an artifact permanently. This also removes
its comments, its shares, and its version history. renderdesk does not
ask a second time, so check the title before you confirm.

## The artifact page

Open an artifact to see a live preview alongside its comments.

### Comments

Comments let you leave feedback for the agent that owns the artifact
to read on its next visit.

1. Enter your comment in the **Start a new thread** box.
2. Select **Comment** to post it.

To reply within an existing thread, enter your reply and select
**Reply**. Select **Resolve** to mark a thread resolved, or **Reopen**
to undo that.

An agent reads your comments through the `list_comments` tool. Treat
the comment box as a message to the agent, not as a way to run code or
change settings.

### Sharing

Sharing gives another renderdesk user read and comment access to an
artifact you own.

1. In the **Shared with** panel, enter the recipient's email address.
2. Select **Share**.

The recipient must already have a renderdesk account. Select
**Unshare** next to a recipient's name to remove their access.

Sharing an artifact does not give the recipient's own MCP connection
any new access. It only adds the artifact to their dashboard.

### Move to another connection

If you own more than one active connection, the artifact page shows a
**Move to another connection** panel. Use it to hand an artifact to a
different agent connection, for example when a new coding agent
continues work another agent started. Select the target connection,
then select **Move**. Only you can do this. No MCP tool can reassign
an artifact.

## Version history

Every update to an artifact creates a new version. Open an artifact and
select **Version history** to see every past version.

- Select a version number to view it as read-only text. renderdesk
  never re-runs an old HTML version.
- Select **Delete** next to a version to remove that one snapshot. You
  cannot delete the current version.
- Select **Prune old versions** to delete every version except the
  current one at once. Use this if an artifact's history takes up more
  space than you want to keep.

## Connections

The **Connections** page lists every client connected to your account,
whether it connected with a personal token or through OAuth.

### Create a personal token

1. Enter a label for the client, such as `laptop` or `opencode`.
2. Select **Create token**.
3. Copy the token. renderdesk shows it only once.

See [Getting started](getting-started.md) for how to use this token in
an MCP client.

### Revoke and delete

Select **Revoke** to disable a connection immediately. A revoked
connection can no longer publish or read artifacts, but its past
artifacts stay in your account.

Select **Delete** to remove a revoked connection from the list.
renderdesk blocks this if the connection still owns an artifact, left a
comment, or shared anything. Move or delete that content first, or
leave the connection in its revoked state.

## Shared with you

The bottom of the **Artifacts** page lists artifacts other users have
shared with you. Select **Open** to comment on one, or **View** to see
it on its own.

## Light and dark theme

Select **Light** or **Dark** in the top bar to switch theme. renderdesk
remembers your choice and applies it across the dashboard and every
artifact page you open, including previews embedded in the dashboard.
