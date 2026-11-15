#!/usr/bin/env bash
# Regression: delayed uvicorn-like readiness must succeed via wait_for_local_healthz
# within 60s (not a fixed sleep 2). No VPS / no real install.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL="${ROOT}/ops/vps/INSTALL_ON_VPS.sh"
DELAY_SEC="${HEALTHZ_TEST_DELAY_SEC:-3}"
MAX_WAIT=60

# Default port can stick from failed Windows runs; pick a free one unless pinned.
pick_test_port() {
  if [ -n "${HEALTHZ_TEST_PORT:-}" ]; then
    echo "$HEALTHZ_TEST_PORT"
    return
  fi
  local p=$((19000 + ($$ % 4000)))
  local n=0
  while [ "$n" -lt 50 ]; do
    if ! curl -fsS --connect-timeout 0.3 --max-time 0.5 "http://127.0.0.1:${p}/api/healthz" >/dev/null 2>&1; then
      echo "$p"
      return
    fi
    p=$((p + 1))
    n=$((n + 1))
  done
  fail "could not find free port for healthz simulation"
}
PORT="$(pick_test_port)"

fail() { echo "FAIL: $*"; exit 1; }
pass() { echo "PASS: $*"; }

bash -n "$INSTALL" || fail "bash -n INSTALL"
pass "bash -n"

# Installer must poll, not sleep 2 before healthz.
if grep -n 'sleep 2' "$INSTALL" | grep -q healthz; then
  fail "sleep 2 still coupled to healthz path"
fi
grep -q 'wait_for_local_healthz' "$INSTALL" || fail "wait_for_local_healthz missing"
grep -q 'report_service_smoke_diagnostics' "$INSTALL" || fail "diagnostics helper missing"
# Ensure the service smoke path calls wait (not bare sleep 2 then curl healthz).
smoke_region="$(awk '/sudo systemctl restart asa-physio.service/,/verify effective SMS/' "$INSTALL")"
printf '%s\n' "$smoke_region" | grep -q 'wait_for_local_healthz' || fail "smoke path missing wait_for_local_healthz"
printf '%s\n' "$smoke_region" | grep -q 'report_service_smoke_diagnostics' || fail "smoke path missing diagnostics on failure"
if printf '%s\n' "$smoke_region" | grep -E '^\s*sleep 2\s*$' | grep -q .; then
  fail "smoke path still has bare sleep 2"
fi
pass "static: poll helpers wired; no bare sleep 2 in smoke"

# Extract wait_for_local_healthz from installer for isolated runtime test.
fn="$(awk '
  /^wait_for_local_healthz\(\)/ {on=1}
  on {print}
  /^report_service_smoke_diagnostics\(\)/ {if(on){exit}}
' "$INSTALL")"
# Drop trailing report function header if captured.
fn="$(printf '%s\n' "$fn" | sed '/^report_service_smoke_diagnostics/,$d')"
[ -n "$fn" ] || fail "could not extract wait_for_local_healthz"
eval "$fn"

command -v curl >/dev/null || fail "curl required"

# Prefer an interpreter that actually runs (Windows may alias python3 to Store stub).
run_py() {
  local c
  for c in python python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys' 2>/dev/null; then
      "$c" "$@"
      return
    fi
  done
  if command -v py >/dev/null 2>&1 && py -3 -c 'import sys' 2>/dev/null; then
    py -3 "$@"
    return
  fi
  fail "python required"
}

# Delayed health server: refuse until DELAY_SEC after start, then 200 on /api/healthz.
run_py - "$PORT" "$DELAY_SEC" <<'PY' &
import sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer

port = int(sys.argv[1])
delay = float(sys.argv[2])
t0 = time.time()

class H(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_GET(self):
        ready = (time.time() - t0) >= delay
        if self.path.startswith("/api/healthz"):
            if not ready:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"starting")
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return
        self.send_response(404)
        self.end_headers()

HTTPServer(("127.0.0.1", port), H).serve_forever()
PY
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  if command -v taskkill >/dev/null 2>&1; then
    taskkill //F //PID "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# Brief settle so bind completes.
sleep 1
# Immediate curl should fail (still in delay window) if DELAY_SEC >= 2.
if [ "$DELAY_SEC" -ge 2 ]; then
  if curl -fsS --connect-timeout 1 --max-time 2 "http://127.0.0.1:${PORT}/api/healthz" >/dev/null 2>&1; then
    fail "server became ready too early for delay simulation"
  fi
  pass "runtime: healthz unavailable during startup delay"
fi

t_start="$(date +%s)"
if ! wait_for_local_healthz "http://127.0.0.1:${PORT}/api/healthz" "$MAX_WAIT"; then
  fail "wait_for_local_healthz timed out (should succeed before ${MAX_WAIT}s)"
fi
t_end="$(date +%s)"
elapsed=$((t_end - t_start))
# Must have waited at least ~DELAY_SEC (allow 1s slack) and finished before MAX_WAIT.
if [ "$elapsed" -lt $((DELAY_SEC - 1)) ]; then
  fail "succeeded too fast (${elapsed}s < delay ${DELAY_SEC}s) — poll may be wrong"
fi
if [ "$elapsed" -ge "$MAX_WAIT" ]; then
  fail "elapsed ${elapsed}s reached max — expected success earlier"
fi
pass "runtime: healthz OK after delayed startup (elapsed=${elapsed}s, delay=${DELAY_SEC}s, max=${MAX_WAIT}s)"

# Owner re-run safety + SMS sink still present (unchanged by this fix).
grep -q 'already present' "$INSTALL" || fail "owner skip path missing"
grep -q 'credentials unchanged' "$INSTALL" || fail "owner no-overwrite wording missing"
grep -q 'REMINDER_BACKGROUND=0' "$INSTALL" || fail "REMINDER_BACKGROUND=0 missing"
grep -q '127.0.0.1:9/asa-sms-sink' "$INSTALL" || fail "SMS sink missing"
pass "static: owner re-run skip + SMS/reminder sinks preserved"

echo "ALL: PASS"
