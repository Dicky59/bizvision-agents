#!/usr/bin/env bash
# Deploy to the production server. Run from Git Bash on your laptop:
#
#   scripts/deploy.sh              # deploy the current commit (must be pushed, tree clean)
#   scripts/deploy.sh <sha|tag>    # deploy / roll back to a specific ref
#
# Env: DEPLOY_HOST (default deploy@5.222.8.228), PUBLIC_URL (default https://agents.bizvision.fi),
#      SKIP_PUBLIC_CHECK=1 to skip the external health check (e.g. before DNS exists).
# Steps on the server: fetch -> checkout ref -> build image -> alembic upgrade head -> up -d ->
# wait until the api container is healthy. Migrations are forward-only: rolling back across a
# migration needs a manual `alembic downgrade` first (the script warns you).
set -euo pipefail

HOST="${DEPLOY_HOST:-deploy@5.222.8.228}"
PUBLIC_URL="${PUBLIC_URL:-https://agents.bizvision.fi}"
APP_DIR=/srv/bizvision-agents

REF="${1:-$(git rev-parse HEAD)}"
[[ "$REF" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "bad ref: $REF" >&2; exit 2; }

if [ -z "${1:-}" ]; then
  git diff --quiet && git diff --cached --quiet \
    || { echo "working tree is dirty: commit first" >&2; exit 1; }
  [ -n "$(git branch -r --contains "$REF" 2>/dev/null)" ] \
    || { echo "$REF is not pushed to origin: git push first" >&2; exit 1; }
fi

echo "==> deploying $REF to $HOST"
ssh "$HOST" bash -s -- "$APP_DIR" "$REF" <<'REMOTE'
set -euo pipefail
APP_DIR="$1"; REF="$2"
cd "$APP_DIR"
[ -f .env ] || { echo "$APP_DIR/.env is missing (see docs/runbook-m7.md)" >&2; exit 1; }
PREV="$(git rev-parse HEAD)"
git fetch --tags --prune origin
git rev-parse --verify --quiet "$REF^{commit}" >/dev/null \
  || { echo "unknown ref on the server: $REF (is it pushed?)" >&2; exit 1; }
git checkout --quiet --detach "$REF"
NEW="$(git rev-parse HEAD)"
echo "previous: $PREV"
echo "now:      $NEW"
if ! git diff --quiet "$PREV" "$NEW" -- alembic/versions; then
  echo "NOTE: migrations changed between $PREV and $NEW."
  echo "      To roll back past them, run 'alembic downgrade' BEFORE deploying the old ref."
fi
C="docker compose -f docker-compose.prod.yml"
$C build
$C run --rm api alembic upgrade head
$C up -d --remove-orphans
echo -n "waiting for api to become healthy"
for _ in $(seq 1 30); do
  status="$(docker inspect --format '{{.State.Health.Status}}' "$($C ps -q api)" 2>/dev/null || true)"
  [ "$status" = "healthy" ] && { echo " ok"; break; }
  echo -n "."; sleep 2
done
[ "${status:-}" = "healthy" ] || { echo; echo "api is not healthy:"; $C logs --tail 50 api; exit 1; }
echo "rollback with: scripts/deploy.sh $PREV"
REMOTE

if [ -z "${SKIP_PUBLIC_CHECK:-}" ]; then
  echo "==> public health check: $PUBLIC_URL/healthz"
  curl -fsS --retry 10 --retry-delay 3 --retry-connrefused "$PUBLIC_URL/healthz"
  echo
fi
echo "deployed."
