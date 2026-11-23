#!/usr/bin/env bash
# Production smoke tests (runbook S10). Run from your laptop in Git Bash:
#
#   read -rs ADMIN_API_KEY && export ADMIN_API_KEY      # prod key; never echoed or logged
#   scripts/smoke.sh                  # all checks (the chat check costs a fraction of a cent)
#   SKIP_CHAT=1 scripts/smoke.sh      # without calling the LLM
#
# Exits non-zero if any check fails. Does NOT submit a lead (do that by hand in the real widget).
set -uo pipefail

BASE="${BASE:-https://agents.bizvision.fi}"
TENANT="${TENANT:-bizvision}"
GOOD_ORIGIN="${GOOD_ORIGIN:-https://www.bizvision.fi}"
fail=0
ok()   { echo "  ok   $*"; }
bad()  { echo "  FAIL $*"; fail=1; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
expect() { # expect <description> <want> <got>
  if [ "$2" = "$3" ]; then ok "$1 ($3)"; else bad "$1: wanted $2, got $3"; fi
}

echo "TLS and basics"
expect "healthz" 200 "$(code "$BASE/healthz")"
expect "http redirects to https" 308 "$(code "http://${BASE#https://}/healthz")"
hdr="$(curl -sI "$BASE/widget.v1.js")"
echo "$hdr" | head -n1 | grep -q 200 && ok "widget.v1.js served" || bad "widget.v1.js not 200"
echo "$hdr" | grep -qi 'cache-control: public, max-age=86400' && ok "widget cache header" || bad "widget cache header"
expect "/docs hidden" 404 "$(code "$BASE/docs")"
expect "/openapi.json hidden" 404 "$(code "$BASE/openapi.json")"

echo "Origin / CORS"
cfg="$BASE/v1/widget/$TENANT/config"
expect "config from allowed origin" 200 "$(code -H "Origin: $GOOD_ORIGIN" "$cfg")"
acao="$(curl -si -H "Origin: $GOOD_ORIGIN" "$cfg" | grep -i '^access-control-allow-origin:' | tr -d '\r')"
[ -n "$acao" ] && ok "CORS header present: $acao" || bad "no Access-Control-Allow-Origin for $GOOD_ORIGIN"
expect "localhost origin rejected" 403 "$(code -H "Origin: http://localhost:5173" "$cfg")"
expect "foreign origin rejected" 403 "$(code -H "Origin: https://evil.example" "$cfg")"

echo "Admin"
expect "admin without key" 401 "$(code "$BASE/v1/admin/tenants/$TENANT")"
if [ -n "${ADMIN_API_KEY:-}" ]; then
  expect "admin with key" 200 "$(code -H "Authorization: Bearer $ADMIN_API_KEY" "$BASE/v1/admin/tenants/$TENANT")"
else
  echo "  skip admin with key (ADMIN_API_KEY not set)"
fi

if [ -z "${SKIP_CHAT:-}" ]; then
  echo "Chat stream (SSE)"
  vid="smoke$(date +%s)"
  out="$(mktemp)"
  curl -sN --max-time 60 -H "Origin: $GOOD_ORIGIN" -H 'Content-Type: application/json' \
    -d "{\"visitor_id\":\"$vid\",\"message\":\"Mitä palveluita tarjoatte?\",\"language\":\"fi\"}" \
    -w '\nfirst-byte=%{time_starttransfer}s total=%{time_total}s\n' \
    "$BASE/v1/assistant/$TENANT/chat" > "$out"
  for ev in meta token done; do
    grep -q "^event: $ev" "$out" && ok "event $ev" || bad "missing SSE event: $ev"
  done
  grep -q '^event: error' "$out" && bad "error event in stream"
  tail -n 1 "$out"
  rm -f "$out"
else
  echo "skip chat (SKIP_CHAT set)"
fi

echo
[ "$fail" -eq 0 ] && echo "ALL SMOKE CHECKS PASSED" || echo "SMOKE CHECKS FAILED"
echo "Still by hand: rate-limit 429 + Retry-After, lead arrives at info@, real iPhone/Android, reboot test (runbook S10)."
exit "$fail"
