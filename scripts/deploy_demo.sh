#!/usr/bin/env bash
# Build this working tree as yarbo-panel:demo ON a remote Docker host and (re)create only the demo instance (:8426,
# its own data dir, DEMO=1: a made-up robot and yard, never the real robot). The live panel (:8425,
# yarbo-panel:latest) is not touched, so changes can be tried, and the browser test run, while someone is using the
# live one.
#   GK_DEPLOY_HOST=root@server GK_DATA_DIR=/srv/groundskeeper scripts/deploy_demo.sh
# GK_DEPLOY_HOST     the SSH target (user@host) that builds and runs the container
# GK_DATA_DIR        the live panel's data directory on that host (the demo reads its .env for the site values)
# GK_DEMO_DATA_DIR   optional: the demo's own data directory (default: GK_DATA_DIR with -demo-yard appended); DEMO=1
#                    refuses a directory that already holds a real robot's data
# GK_DEMO_URL        optional: the demo's address for links (default http://<host part of GK_DEPLOY_HOST>:8426)
set -euo pipefail
: "${GK_DEPLOY_HOST:?set GK_DEPLOY_HOST to the SSH target that runs Docker (e.g. root@server)}"
: "${GK_DATA_DIR:?set GK_DATA_DIR to the data directory of the live panel on that host}"
HOST=$GK_DEPLOY_HOST
PORT=8426
DEMO_DATA=${GK_DEMO_DATA_DIR:-${GK_DATA_DIR%/}-demo-yard}
DEMO_URL=${GK_DEMO_URL:-http://${HOST#*@}:$PORT}

cd "$(dirname "$0")/.."
BUILD=$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- app catalog web docs/user CHANGELOG.md || echo -dirty)
echo "==> building yarbo-panel:demo on $HOST"
tar --exclude=.venv --exclude=data --exclude=web/vendor --exclude=__pycache__ --exclude=.pytest_cache \
    --exclude=.git -cf - Dockerfile .dockerignore requirements.txt docker app catalog web docs/user CHANGELOG.md \
    LICENSE THIRD_PARTY_NOTICES.md \
  | ssh "$HOST" "docker build -q --build-arg PANEL_BUILD=$BUILD -t yarbo-panel:demo - >/dev/null && echo built"

echo "==> recreating yarbo-panel-demo"
# Site values (the time zone) come from the live panel's env file; DEMO=1 ignores its robot address and integrations.
ssh "$HOST" "docker rm -f yarbo-panel-demo >/dev/null 2>&1; docker run -d --name yarbo-panel-demo --network host \
  --restart unless-stopped --env-file ${GK_DATA_DIR%/}/.env \
  -e DATA_DIR=/data -e PORT=$PORT -e DEMO=1 -e PANEL_AUTH=off -e POLL_IDLE_S=600 -e PANEL_URL=$DEMO_URL -e HA_URL= -e HA_TOKEN= \
  -v $DEMO_DATA:/data yarbo-panel:demo >/dev/null"

for _ in $(seq 1 45); do
  if ssh "$HOST" "curl -fsS -m 3 http://127.0.0.1:$PORT/health" 2>/dev/null; then
    echo; echo "==> demo is up at $DEMO_URL"
    exit 0
  fi
  sleep 2
done
echo "demo did not come up; logs:"; ssh "$HOST" "docker logs --tail 60 yarbo-panel-demo"; exit 1
