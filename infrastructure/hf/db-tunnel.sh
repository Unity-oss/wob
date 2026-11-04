#!/bin/sh
# Keeps an SSH tunnel open to the droplet's Postgres (infrastructure/deployment/compose.yaml
# publishes it on 127.0.0.1:5432 there, reachable only through the restricted dbtunnel user).
# autossh restarts the tunnel if it drops; DATABASE_URL on this side just points at
# 127.0.0.1:5432 as if Postgres were local.
set -eu

: "${DB_TUNNEL_HOST:?set DB_TUNNEL_HOST, the droplet IP}"
: "${DB_TUNNEL_SSH_KEY:?set DB_TUNNEL_SSH_KEY, the dbtunnel private key}"

KEY_FILE=/run/db-tunnel-key
umask 077
printf '%s\n' "$DB_TUNNEL_SSH_KEY" >"$KEY_FILE"
chmod 600 "$KEY_FILE"

export AUTOSSH_GATETIME=0
exec autossh -M 0 -N \
  -o StrictHostKeyChecking=accept-new \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -i "$KEY_FILE" \
  -L "127.0.0.1:5432:127.0.0.1:5432" \
  "${DB_TUNNEL_USER:-dbtunnel}@${DB_TUNNEL_HOST}"
