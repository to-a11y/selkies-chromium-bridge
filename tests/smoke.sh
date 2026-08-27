#!/usr/bin/env bash

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FAILED=0

ok() {
    printf '[ OK ] %s\n' "$1"
}

fail() {
    printf '[FAIL] %s\n' "$1"
    FAILED=1
}

# Read public port from .env unless explicitly supplied.
PORT="${PORT:-}"

if [ -z "$PORT" ] && [ -f .env ]; then
    PORT="$(sed -n 's/^PORT=//p' .env | tail -1)"
fi

PORT="${PORT:-8080}"
PUBLIC_URL="${PUBLIC_URL:-http://127.0.0.1:${PORT}}"

CONTAINER="${CONTAINER:-}"

if [ -z "$CONTAINER" ]; then
    CONTAINER="$(docker compose ps -q browser 2>/dev/null || true)"
fi

echo "=== Selkies Chromium Bridge smoke test ==="
echo "Public URL: $PUBLIC_URL"
echo

if [ -n "$CONTAINER" ] &&
   docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null |
   grep -qx true; then
    ok "browser container is running"
else
    fail "browser container is not running"
fi

if curl -fsS --max-time 5 "$PUBLIC_URL/" >/dev/null; then
    ok "gateway HTTP"
else
    fail "gateway HTTP"
fi

if curl -fsS --max-time 5 "$PUBLIC_URL/" |
   grep -q '/filebridge.js'; then
    ok "FileBridge script injection"
else
    fail "FileBridge script injection"
fi

if docker exec "$CONTAINER" \
   curl -fsS --max-time 5 \
   http://127.0.0.1:9231/health |
   grep -q '"ok": true'; then
    ok "FileBridge health"
else
    fail "FileBridge health"
fi

if docker exec "$CONTAINER" \
   bash -lc 'DISPLAY=:99 xdpyinfo >/dev/null 2>&1'; then
    ok "X11 display"
else
    fail "X11 display"
fi

if docker exec "$CONTAINER" \
   bash -lc 'pgrep -f google-chrome-stable >/dev/null'; then
    ok "Chrome process"
else
    fail "Chrome process"
fi

if docker exec "$CONTAINER" \
   curl -fsS --max-time 3 \
   http://127.0.0.1:9222/json/version >/dev/null; then
    ok "Chrome DevTools Protocol"
else
    fail "Chrome DevTools Protocol"
fi

if docker exec "$CONTAINER" \
   bash -lc 'pgrep -f "/usr/local/bin/selkies" >/dev/null'; then
    ok "Selkies process"
else
    fail "Selkies process"
fi

if docker exec "$CONTAINER" \
   bash -lc \
   'PULSE_SERVER=unix:/run/pulse/native pactl info >/dev/null 2>&1'; then
    ok "PulseAudio"
else
    fail "PulseAudio"
fi

if docker exec "$CONTAINER" \
   bash -lc \
   'PULSE_SERVER=unix:/run/pulse/native pactl list short sources |
    grep -q "output.monitor"'; then
    ok "audio monitor source"
else
    fail "audio monitor source"
fi

if docker logs "$CONTAINER" 2>&1 |
   grep -q 'FATAL:.*video pipeline did not start'; then
    fail "video pipeline startup"
else
    ok "no fatal video startup error"
fi

echo

if [ "$FAILED" -eq 0 ]; then
    echo "ALL SMOKE TESTS PASSED"
else
    echo "SMOKE TEST FAILED"
fi

exit "$FAILED"
