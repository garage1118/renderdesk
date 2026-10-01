# Configuration

renderdesk reads its configuration from environment variables, all
prefixed `RENDERDESK_`. Set them on the container (`-e` flags, a compose
file, or your orchestrator's environment settings). When running from
source, a `.env` file in the working directory also works — see
`.env.example` in the repository.

## Required

| Variable | Description |
|---|---|
| `RENDERDESK_PUBLIC_BASE_URL` | The URL users reach renderdesk at, e.g. `https://renderdesk.example.com`. Every link and OAuth URL the app generates is built from it, so it must be the public HTTPS URL, not the container's internal address. No trailing slash. |
| `RENDERDESK_AUTH_SCHEME` | How dashboard users log in: `password` or `oidc`. Locked in on first boot — every later start must use the same value. To change it, see [`set-auth-scheme`](cli-reference.md#set-auth-scheme). |

renderdesk refuses to start if either is missing.

## Behind a reverse proxy

| Variable | Default | Description |
|---|---|---|
| `RENDERDESK_TRUSTED_PROXY_IPS` | unset | The IP address or CIDR of your reverse proxy. Several values can be comma-separated. |

Set this whenever renderdesk sits behind a reverse proxy (nginx, Caddy,
Traefik, Nginx Proxy Manager, and so on). renderdesk only believes the
proxy's `X-Forwarded-For` and `X-Forwarded-Proto` headers when the
request comes from an address listed here. If it is unset or wrong:

- **Every visitor appears to come from the proxy's address**, so all
  users share one rate-limit bucket. A few failed logins from anyone can
  lock everyone out for a while.
- **Redirects point at `http://`** instead of `https://`, and some
  clients refuse to follow them.

renderdesk logs a warning at startup when this is unset.

Prefer a value that survives containers being recreated. Docker can give
a container a new IP address each time it is recreated, so a single
hard-coded proxy IP can silently stop matching. Use the Docker network's
subnet as a CIDR (find it with `docker network inspect <network>`), as
long as only the proxy can reach renderdesk on that network, or give the
proxy container a fixed IP address.

`*` is not allowed — renderdesk refuses to start with it, because it lets
any client forge its own IP address and bypass rate limits.

Leave it unset only if renderdesk is reachable directly, with no proxy in
front of it.

## Single sign-on (OIDC)

Required when `RENDERDESK_AUTH_SCHEME=oidc`; ignored otherwise. See
[Single sign-on](cli-reference.md#single-sign-on-oidc) for linking
existing accounts.

| Variable | Default | Description |
|---|---|---|
| `RENDERDESK_OIDC_ISSUER_URL` | — | Your identity provider's issuer URL. |
| `RENDERDESK_OIDC_CLIENT_ID` | — | The client ID registered with your identity provider. |
| `RENDERDESK_OIDC_CLIENT_SECRET` | — | The matching client secret. |
| `RENDERDESK_OIDC_ALLOW_SIGNUP` | `false` | When `true`, a first-time login from an unknown identity creates a new account. When `false`, accounts must be created with [`create-user`](cli-reference.md#create-user) first. |

## Limits

These apply per connection (one personal token or one OAuth-authorized
client) or per artifact, as named.

| Variable | Default | Description |
|---|---|---|
| `RENDERDESK_MAX_ARTIFACTS_PER_CONNECTION` | `200` | Artifacts one connection can own. |
| `RENDERDESK_MAX_BYTES_PER_ARTIFACT` | `2000000` | Largest single artifact, in bytes (about 2 MB). |
| `RENDERDESK_MAX_TOTAL_BYTES_PER_CONNECTION` | `50000000` | Total storage per connection, in bytes (about 50 MB), version history included. |
| `RENDERDESK_MAX_VERSIONS_PER_ARTIFACT` | `20` | Versions kept per artifact. Older versions are deleted when a new one is written. |
| `RENDERDESK_MAX_COMMENTS_PER_ARTIFACT` | `500` | Comments allowed on one artifact. |
| `RENDERDESK_MAX_COMMENT_BYTES_PER_ARTIFACT` | `2000000` | Total size of all comments on one artifact, in bytes. |

## Expiry and storage

| Variable | Default | Description |
|---|---|---|
| `RENDERDESK_TOKEN_EXPIRY_DAYS` | `90` | How long a personal token lasts after it is created. |
| `RENDERDESK_SESSION_EXPIRY_DAYS` | `30` | How long a dashboard login lasts. |
| `RENDERDESK_DATABASE_PATH` | `./data/renderdesk.db` | Where the SQLite database lives. In the Docker image this resolves to `/app/data/renderdesk.db` — mount a persistent volume at `/app/data`, or every restart loses all data. |
