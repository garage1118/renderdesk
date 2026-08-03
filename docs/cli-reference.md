# CLI reference

The `renderdesk` command line runs inside the deployed container. It is
the only way to provision the first user, force-delete something the
dashboard won't let you delete, or change how the instance authenticates
— shell access to the container is the trust boundary for all of it, so
none of these commands exist as MCP tools or dashboard buttons.

Every command here is meant to run non-interactively — most take
`--email`, `--id`, or similar flags rather than asking questions, so an
agent with container access can run them too. `create-user` is the one
exception: it always prompts for a password.

## Running a command

```
docker exec <container> renderdesk <command> [options]
```

Replace `<container>` with whatever the renderdesk container is actually
named in your deployment. The examples below use `renderdesk`.

`docker exec` needs `-it` (allocate a terminal, keep stdin open) for
`create-user` specifically, since it's the one command that actually
prompts for input — without it, there's no terminal for the password
prompt to read from, and the command fails immediately with `Aborted!`.
The other commands don't need `-it`; none of them read from stdin.

## Provisioning

### create-user

Creates a human user who can log into the web dashboard. There is no
self-serve signup — every account starts here, or through a first OIDC
login if signup is enabled (see [Single sign-on](#single-sign-on-oidc)
below).

```
docker exec -it renderdesk renderdesk create-user --email alice@example.com
```

Prompts for a password (minimum 8 characters, maximum 72 bytes — a
bcrypt limit) with confirmation.

### create-token

Issues a new personal MCP bearer token for a user. Equivalent to
creating one from the dashboard's **Connections** page while logged in
as that user.

```
docker exec renderdesk renderdesk create-token --email alice@example.com --label laptop
```

`--label` is optional. The token is printed once — save it immediately,
renderdesk never shows it again.

## Deleting an artifact by force

### delete-artifact

Permanently deletes any artifact by id, bypassing the per-owner scoping
the dashboard enforces. `--title` and `--email` must exactly match the
artifact's actual title and owner or nothing is deleted:

```
docker exec renderdesk renderdesk delete-artifact \
  --id <artifact-id> \
  --title "Exact Title Here" \
  --email alice@example.com
```

Find the id in the artifact's dashboard URL (`/dashboard/a/<id>`). Pass
`--title ""` for an untitled artifact. See
[why this isn't a confirmation prompt](#why-exact-matches-instead-of-a-yn-prompt)
below.

## Managing connections

Both commands need the connection's id and its owner's exact email —
find both on the dashboard's **Connections** page.

### revoke-token

Kills a connection's access immediately (idempotent if already revoked)
while keeping its published history:

```
docker exec renderdesk renderdesk revoke-token \
  --id <connection-id> \
  --label "laptop" \
  --email alice@example.com
```

Pass `--label ""` if the connection has no label.

### delete-token

Permanently removes a revoked connection from the list. Blocked if the
connection ever published an artifact, left a comment, or shared
something — run `revoke-token` first, and move or delete that content
if it should go too:

```
docker exec renderdesk renderdesk delete-token \
  --id <connection-id> \
  --label "laptop" \
  --email alice@example.com
```

## Single sign-on (OIDC)

A first-time OIDC login is deliberately never auto-linked to an
existing account just because the emails match — that would let anyone
who controls an identity provider account's email claim silently take
over a matching local account. These two commands are the manual
alternative.

### link-oidc-identity

Links an external OIDC identity to an existing renderdesk user, so they
can log in through your identity provider instead of, or alongside, a
password:

```
docker exec renderdesk renderdesk link-oidc-identity \
  --email alice@example.com \
  --issuer "https://idp.example.com/application/o/renderdesk/" \
  --subject "alice"
```

- `--issuer` must match, character for character, the `"issuer"` field
  from your identity provider's own
  `/.well-known/openid-configuration` document — don't type it from
  memory, copy it.
- `--subject` is whatever your identity provider puts in the token's
  `sub` claim for that person. This depends entirely on how the
  provider is configured (for example, Authentik has a
  provider-level **Subject mode** setting — "based on username" is the
  simplest choice, since you already know that value, versus a
  hashed or UUID-based subject you'd have to look up separately).

Run this once per identity provider account a person will actually log
in as. If someone might authenticate as more than one account there
(an admin/break-glass account and a personal one, for instance), link
each one you want to work — an unlinked one just gets rejected at login
with "No renderdesk account exists yet for you."

### unlink-oidc-identity

Removes a previously linked identity — to clean up a mistaken
`link-oidc-identity` call, or to revoke SSO access for one identity
provider account without touching a user's other logins or their
password:

```
docker exec renderdesk renderdesk unlink-oidc-identity \
  --issuer "https://idp.example.com/application/o/renderdesk/" \
  --subject "alice" \
  --email alice@example.com
```

`--email` must match the identity's currently linked user, same
exact-match reasoning as everything else here.

## Changing the auth scheme

### set-auth-scheme

The auth scheme (`password`, `oidc`, or `saml`) is locked in on first
boot and checked on every boot after — the app refuses to start if the
`RENDERDESK_AUTH_SCHEME` environment variable doesn't match what's
already persisted. This is deliberate: it stops a scheme change from
happening by accident, just by editing an environment variable (for
example, silently falling back from SSO to password auth for an
account that has no meaningful password set).

To change it on purpose:

```
docker exec renderdesk renderdesk set-auth-scheme oidc
```

1. Run this command first, against the container still running the
   *old* scheme.
2. Then update `RENDERDESK_AUTH_SCHEME` (and, for `oidc`, the
   `RENDERDESK_OIDC_ISSUER_URL` / `RENDERDESK_OIDC_CLIENT_ID` /
   `RENDERDESK_OIDC_CLIENT_SECRET` variables) in your deployment
   config.
3. Restart. The new environment value now matches what step 1 already
   persisted, so startup succeeds.

Doing it out of order — changing the environment variable before
running this command — makes the app refuse to boot at all.

## Why exact matches instead of a y/N prompt

`delete-artifact`, `revoke-token`, `delete-token`, and
`unlink-oidc-identity` all ask for fields that describe the thing
you're acting on (a title, a label, an owner's email) rather than a
"Are you sure? [y/N]" prompt. That's on purpose: these commands are
expected to run non-interactively, often invoked by an agent with
container access rather than a human sitting at a terminal, so there's
no one to read a confirmation prompt and answer it.

Requiring the caller to already know the correct title, label, or
email catches a wrong or copy-pasted id before it does damage. If the
fields don't match, the command fails without saying which field was
wrong — revealing that would turn the check into a guessing oracle
instead of a real assertion that the caller had the right thing in
mind to begin with.
