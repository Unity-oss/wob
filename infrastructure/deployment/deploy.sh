#!/usr/bin/env bash
# Build the images here, ship them and the stack config to the server, and restart the stack.
#   infrastructure/deployment/deploy.sh root@139.59.160.17
# Images are built locally (Docker must be running) because building on the server alongside the
# running stack exhausted its memory. Secrets live on the server in /etc/wob/env (mode 600),
# never in the repository.
set -euo pipefail
HOST=${1:?usage: deploy.sh user@host}
KEY=${SSH_KEY:-$HOME/.ssh/wob_do}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SSH="ssh -i $KEY"

docker build --platform linux/amd64 -t wob-backend:latest "$ROOT/backend"
docker build --platform linux/amd64 --build-arg API_URL=http://api:8000 -t wob-web:latest "$ROOT/frontend"

docker save wob-backend:latest wob-web:latest | gzip | $SSH "$HOST" 'gunzip | docker load'

rsync -az --delete -e "$SSH" --exclude '.DS_Store' \
  "$ROOT/infrastructure/" "$HOST:/opt/wob/infrastructure/"

$SSH "$HOST" 'cd /opt/wob/infrastructure/deployment \
  && docker compose --env-file /etc/wob/env up -d --no-build --remove-orphans \
  && docker image prune -f >/dev/null \
  && docker compose --env-file /etc/wob/env ps --format "{{.Service}}: {{.Status}}"'

# Once the API is up (so migrations have run), accept credible findings left over from earlier
# research, link them to the claims they support, and verify claims supporting documents back
# (jury decision). Each skips work already done, so they run on every deploy.
$SSH "$HOST" 'cd /opt/wob/infrastructure/deployment \
  && for i in $(seq 60); do
       docker compose --env-file /etc/wob/env exec -T api python -c \
         "import urllib.request; urllib.request.urlopen(\"http://localhost:8000/api/v1/health\")" \
         >/dev/null 2>&1 && break
       sleep 5
     done \
  && docker compose --env-file /etc/wob/env exec -T api python -m app.cli auto-accept-research \
  && docker compose --env-file /etc/wob/env exec -T api python -m app.cli match-research-claims \
  && docker compose --env-file /etc/wob/env exec -T api python -m app.cli verify-supporting-claims'
