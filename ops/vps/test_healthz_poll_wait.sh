#!/usr/bin/env bash
# Regression: wait_for_local_healthz uses wall-clock deadline (not iteration×curl).
# - delayed success within 60s
# - timeout honours max_sec via controllable clock (no real 60s wait)
# No VPS / no real install.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL="${ROOT}/ops/vps/INSTALL_ON_VPS.sh"
DELAY_SEC="${HEALTHZ_TEST_DELAY_SEC:-3}"
MAX_WAIT=60

fail() { echo "FAIL: $*"; exit 1; }
pass() { echo "PASS: $*"; }

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

bash -n "$INSTALL" || fail "bash -n INSTALL"
pass "bash -n"

grep -q 'wait_for_local_healthz' "$INSTALL" || fail "wait_for_local_healthz missing"
grep -q 'report_service_smoke_diagnostics' "$INSTALL" || fail "diagnostics helper missing"
grep -q 'wall clock' "$INSTALL" || fail "wall-clock deadline wording missing"
grep -q '_asa_healthz_now' "$INSTALL" || fail "clock hook missing"
grep -q '_asa_healthz_sleep' "$INSTALL" || fail "sleep hook missing"

# Must not use iteration counter as the sole deadline (old bug: i -lt max_sec).
fn_body="$(awk '
  /^wait_for_local_healthz\(\)/ {on=1}
  on {print}
  /^report_service_smoke_diagnostics\(\)/ {if(on){exit}}
' "$INSTALL")"
fn_body="$(printf '%s\n' "$fn_body" | sed '/^report_service_smoke_diagnostics/,$d')"
if printf '%s\n' "$fn_body" | grep -E 'while\s+\[\s*"\$i"\s+-lt' | grep -q .; then
  fail "iteration-count while-loop still present"
fi
printf '%s\n' "$fn_body" | grep -q 'deadline=' || fail "deadline variable missing"
smoke_region="$(awk '/sudo systemctl restart asa-physio.service/,/verify effective SMS/' "$INSTALL")"
printf '%s\n' "$smoke_region" | grep -q 'wait_for_local_healthz' || fail "smoke path missing wait_for_local_healthz"
printf '%s\n' "$smoke_region" | grep -q 'report_service_smoke_diagnostics' || fail "smoke path missing diagnostics on failure"
if printf '%s\n' "$smoke_region" | grep -E '^\s*sleep 2\s*$' | grep -q .; then
  fail "smoke path still has bare sleep 2"
fi
pass "static: wall-clock deadline + poll helpers wired"

# Extract clock hooks + wait_for_local_healthz (stop before diagnostics).
extract="$(awk '
  /^_asa_healthz_now\(\)/ {on=1}
  /^wait_for_local_healthz\(\)/ {on=1}
  on {print}
  /^report_service_smoke_diagnostics\(\)/ {if(on){exit}}
' "$INSTALL")"
extract="$(printf '%s\n' "$extract" | sed '/^report_service_smoke_diagnostics/,$d')"
[ -n "$extract" ] || fail "could not extract wait_for_local_healthz"
eval "$extract"

command -v curl >/dev/null || fail "curl required"

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

# --- Runtime A: delayed success (real short delay) ---
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

sleep 1
if [ "$DELAY_SEC" -ge 2 ]; then
  if curl -fsS --connect-timeout 1 --max-time 1 "http://127.0.0.1:${PORT}/api/healthz" >/dev/null 2>&1; then
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
if [ "$elapsed" -lt $((DELAY_SEC - 1)) ]; then
  fail "succeeded too fast (${elapsed}s < delay ${DELAY_SEC}s)"
fi
if [ "$elapsed" -ge "$MAX_WAIT" ]; then
  fail "elapsed ${elapsed}s reached max — expected success earlier"
fi
pass "runtime: healthz OK after delayed startup (elapsed=${elapsed}s, delay=${DELAY_SEC}s, max=${MAX_WAIT}s)"

cleanup
trap - EXIT

# --- Runtime B: timeout uses wall-clock max_sec (fake clock; no real 60s wait) ---
FAKE_NOW=1000000
FAKE_SLEEPS=0
_asa_healthz_now() { echo "$FAKE_NOW"; }
_asa_healthz_sleep() {
  local n="${1:-1}"
  FAKE_NOW=$((FAKE_NOW + n))
  FAKE_SLEEPS=$((FAKE_SLEEPS + 1))
}
# Instant-fail curl so connect-timeout does not burn real seconds (clock is fake).
curl() { return 7; }

wall_a="$(date +%s)"
set +e
wait_for_local_healthz "http://127.0.0.1:9/api/healthz" 60
rc=$?
set -e
wall_b="$(date +%s)"
wall_elapsed=$((wall_b - wall_a))
fake_elapsed=$((FAKE_NOW - 1000000))

[ "$rc" -eq 1 ] || fail "expected timeout rc=1, got $rc"
# Fake clock must advance to ~60s deadline (allow 59..60 if last curl ate remaining).
if [ "$fake_elapsed" -lt 59 ] || [ "$fake_elapsed" -gt 60 ]; then
  fail "fake elapsed=${fake_elapsed}s — expected ~60s wall-clock deadline"
fi
# Must not have waited ~60s of real time (fake sleep + instant curl).
if [ "$wall_elapsed" -ge 15 ]; then
  fail "wall elapsed ${wall_elapsed}s too long — fake clock/curl not used"
fi
pass "runtime: timeout respects 60s wall-clock deadline via fake clock (fake=${fake_elapsed}s wall=${wall_elapsed}s sleeps=${FAKE_SLEEPS})"

# Static preserves (unchanged by this fix).
grep -q 'already present' "$INSTALL" || fail "owner skip path missing"
grep -q 'credentials unchanged' "$INSTALL" || fail "owner no-overwrite wording missing"
grep -q 'REMINDER_BACKGROUND=0' "$INSTALL" || fail "REMINDER_BACKGROUND=0 missing"
grep -q '127.0.0.1:9/asa-sms-sink' "$INSTALL" || fail "SMS sink missing"
pass "static: owner re-run skip + SMS/reminder sinks preserved"

echo "ALL: PASS"
