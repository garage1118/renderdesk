#!/bin/sh
set -e

# Runs as root (the image's default user) so it can fix up ownership of the
# persistent volume regardless of what previously wrote to it — including a
# volume left behind by an older image version that ran entirely as root —
# then drops to the unprivileged appuser for the actual server process.
mkdir -p /app/data
chown -R appuser:appuser /app/data

# RENDERDESK_TRUSTED_PROXY_IPS scopes which reverse-proxy address(es)
# uvicorn trusts to set X-Forwarded-For/X-Forwarded-Proto — set it to the
# actual proxy's IP or CIDR in the deploy environment (Portainer stack env),
# never baked into the image, since it's specific to wherever this is
# running. Left unset, uvicorn falls back to its own default of trusting
# only 127.0.0.1 — safe (if the proxy isn't there, forwarded headers are
# just ignored, not a startup failure) rather than the old blanket "*".
if [ -n "$RENDERDESK_TRUSTED_PROXY_IPS" ]; then
    set -- "$@" --forwarded-allow-ips "$RENDERDESK_TRUSTED_PROXY_IPS"
fi

exec gosu appuser "$@"
