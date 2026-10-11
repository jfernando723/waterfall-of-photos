#!/usr/bin/env bash
# Stop whatever is serving on the dev port and start the current code again.
cd "$(dirname "$0")/.."
LOG=".devdata/server-${WATERFALL_PORT:-7275}.log"
PIDS="$(lsof -ti tcp:${WATERFALL_PORT:-7275} -sTCP:LISTEN 2>/dev/null || true)"
[[ -n "$PIDS" ]] && kill $PIDS 2>/dev/null
for _ in 1 2 3 4 5 6 7 8 9 10; do lsof -ti tcp:${WATERFALL_PORT:-7275} -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 0.3; done
nohup bash scripts/dev.sh > "$LOG" 2>&1 &
for _ in $(seq 1 40); do curl -s -m 1 "http://127.0.0.1:${WATERFALL_PORT:-7275}/healthz" >/dev/null && { echo "serving: $(curl -s http://127.0.0.1:${WATERFALL_PORT:-7275}/healthz)"; exit 0; }; sleep 0.25; done
echo "did not come up:"; cat "$LOG"; exit 1
