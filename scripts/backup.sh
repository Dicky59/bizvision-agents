#!/usr/bin/env bash
# Weekly logical backup of the Neon database (ADR-001, ADR-008). Runs on the server as `deploy`
# from the bv-backup systemd timer, or by hand:  scripts/backup.sh
# Output: /var/backups/bizvision/bv-YYYYMMDD-HHMM.dump (custom format, mode 600), kept 28 days.
# Uses a pinned Postgres 16 client (same major as Neon) via Docker. The dump contains personal
# data (leads, conversations): keep it on this server only until an off-box target exists.
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/bizvision-agents}"
OUT_DIR="${OUT_DIR:-/var/backups/bizvision}"
KEEP_DAYS="${KEEP_DAYS:-28}"
umask 077

url="$(grep -E '^DATABASE_URL_DIRECT=' "$APP_DIR/.env" | head -n1 | cut -d= -f2-)"
url="${url%\"}"; url="${url#\"}"
[ -n "$url" ] || { echo "DATABASE_URL_DIRECT missing in $APP_DIR/.env" >&2; exit 1; }
# SQLAlchemy scheme -> libpq scheme. libpq understands sslmode and channel_binding as-is.
export DUMP_URL="${url/postgresql+asyncpg:/postgresql:}"

stamp="$(date -u +%Y%m%d-%H%M)"
tmp="$OUT_DIR/.bv-$stamp.dump.partial"
final="$OUT_DIR/bv-$stamp.dump"
mkdir -p "$OUT_DIR"
trap 'rm -f "$tmp"' EXIT

docker run --rm -e DUMP_URL postgres:16-alpine \
  sh -c 'pg_dump --format=custom --no-owner --no-privileges "$DUMP_URL"' > "$tmp"

# Sanity: the archive must be readable and contain tables.
docker run --rm -i postgres:16-alpine pg_restore --list < "$tmp" | grep -q "TABLE DATA" \
  || { echo "backup looks empty or corrupt" >&2; exit 1; }

mv "$tmp" "$final"
trap - EXIT
find "$OUT_DIR" -name 'bv-*.dump' -mtime +"$KEEP_DAYS" -delete
echo "backup ok: $final ($(du -h "$final" | cut -f1)); kept: $(ls "$OUT_DIR"/bv-*.dump | wc -l)"
