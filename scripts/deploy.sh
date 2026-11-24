#!/usr/bin/env bash
# Build the panel ON a remote Docker host from this working tree (tar | docker build -) and (re)start its Compose
# stack there (deploy/docker-compose.unraid.yml, with its data path set to GK_DATA_DIR). Needs only key-based SSH
# here; no local Docker.
#   GK_DEPLOY_HOST=root@server GK_DATA_DIR=/srv/groundskeeper GK_COMPOSE_DIR=/srv/compose/groundskeeper #     scripts/deploy.sh
#   FORCE=1 scripts/deploy.sh   (skip the two checks below)
# GK_DEPLOY_HOST  the SSH target (user@host) that builds and runs the container
# GK_DATA_DIR     the panel's data directory on that host (mounted at /data; its .env holds the optional defaults)
# GK_COMPOSE_DIR  the directory on that host that holds the stack's docker-compose.yml (on Unraid: the Compose
#                 Manager plugin's project folder for the stack)
# GK_PANEL_URL    optional: where this machine reaches the panel (default http://<host part of GK_DEPLOY_HOST>:8425)
set -euo pipefail
: "${GK_DEPLOY_HOST:?set GK_DEPLOY_HOST to the SSH target that runs Docker (e.g. root@server)}"
: "${GK_DATA_DIR:?set GK_DATA_DIR to the data directory of the panel on that host}"
: "${GK_COMPOSE_DIR:?set GK_COMPOSE_DIR to the directory on that host for the docker-compose.yml of the stack}"
HOST=$GK_DEPLOY_HOST
APPDATA=$GK_DATA_DIR
PROJECT=$GK_COMPOSE_DIR
PORT=8425
PANEL=${GK_PANEL_URL:-http://${HOST#*@}:$PORT}

cd "$(dirname "$0")/.."
BUILD=$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- app catalog web docs/user CHANGELOG.md || echo -dirty)
TAG=$(date +%Y%m%d-%H%M%S)

# Two checks against the panel that is running now (skipped when it cannot be reached, e.g. it is down):
# 1. no deploy mid-mow: a restart takes the guards and the stall alert away for ~10 s (run 4 had five);
# 2. no rollback: several sessions deploy from their own worktrees, and a tree without the live build's commit
#    would silently replace it (2026-10-10 13:10).
if [ "${FORCE:-}" != 1 ]; then
  # /health says "busy" without signing in; panels from before sign-in (no "busy") answer /api/status instead.
  HEALTH=$(curl -fsS -m 5 "$PANEL/health" 2>/dev/null || true)
  if [ -n "$HEALTH" ]; then
    MOWING=$(printf '%s' "$HEALTH" | python -c "import json,sys; d=json.load(sys.stdin); print(('yes' if d['busy'] else 'no') if 'busy' in d else 'old')" 2>/dev/null || true)
    if [ "$MOWING" = old ]; then
      MOWING=$(curl -fsS -m 5 "$PANEL/api/status" 2>/dev/null | python -c "import json,sys; d=json.load(sys.stdin); print('yes' if d.get('run') or (d.get('plan') or {}).get('active') else 'no')" 2>/dev/null || true)
    fi
    if [ "$MOWING" != no ]; then          # "yes", or the answer could not be read (no python?): do not guess
      echo "refusing: a plan is running, or the panel's state could not be read (deploy after it ends, or FORCE=1)"; exit 2
    fi
  fi
  LIVE=$(curl -fsS -m 5 "$PANEL/health" 2>/dev/null | python -c "import json,sys; print(json.load(sys.stdin).get('build','').split('-')[0])" 2>/dev/null || true)
  if [ -n "$LIVE" ] && ! git merge-base --is-ancestor "$LIVE" HEAD 2>/dev/null; then
    echo "refusing: the live build $LIVE is not in this branch; merge it first (git fetch; git merge), or FORCE=1"; exit 2
  fi
fi

echo "==> building yarbo-panel:$TAG on $HOST"
tar --exclude=.venv --exclude=data --exclude=web/vendor --exclude=__pycache__ --exclude=.pytest_cache \
    --exclude=.git -cf - Dockerfile .dockerignore requirements.txt docker app catalog web docs/user CHANGELOG.md \
    LICENSE THIRD_PARTY_NOTICES.md \
  | ssh "$HOST" "docker build -q --build-arg PANEL_BUILD=$BUILD -t yarbo-panel:$TAG -t yarbo-panel:latest - >/dev/null && echo built"

echo "==> stack files"
ssh "$HOST" "mkdir -p $APPDATA $PROJECT"
sed "s#/mnt/user/appdata/yarbo-panel#$APPDATA#g" deploy/docker-compose.unraid.yml | ssh "$HOST" "cat > $PROJECT/docker-compose.yml"
ssh "$HOST" "echo yarbo-panel > $PROJECT/name"    # the stack's name in Unraid's Compose Manager (ignored elsewhere)

echo "==> starting"
ssh "$HOST" "cd $PROJECT && docker compose up -d --remove-orphans 2>&1 | tail -2"

echo "==> waiting for /health"
for _ in $(seq 1 45); do
  if ssh "$HOST" "curl -fsS -m 3 http://127.0.0.1:$PORT/health" 2>/dev/null; then
    echo; echo "==> yarbo-panel:$TAG is up at $PANEL"
    # Every deploy leaves a dated tag; keep this one and the previous (for a rollback), drop the rest.
    ssh "$HOST" "docker images yarbo-panel --format '{{.Tag}}' | grep -E '^[0-9]{8}-[0-9]{6}$' | sort -r | tail -n +3       | sed 's/^/yarbo-panel:/' | xargs -r docker rmi >/dev/null 2>&1; echo \"==> images kept: \$(docker images yarbo-panel --format '{{.Tag}}' | tr '\n' ' ')\""
    exit 0
  fi
  sleep 2
done
echo "yarbo-panel did not come up; logs:"; ssh "$HOST" "docker logs --tail 60 yarbo-panel"; exit 1
