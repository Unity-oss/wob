#!/bin/bash
# wait-for.sh HOST PORT -- CMD...   Blocks until HOST:PORT accepts a TCP connection, then execs
# CMD. Used so api/worker don't race the db-tunnel and clamd startups.
set -euo pipefail
host=$1 port=$2
shift 2
[ "${1:-}" = "--" ] && shift
until (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null; do
  sleep 1
done
exec "$@"
