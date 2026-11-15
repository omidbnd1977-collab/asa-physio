#!/usr/bin/env bash
# Regression: wait_for_local_healthz high-res monotonic deadline.
# - formatter never exceeds input (truncate-down)
# - delayed success within 60s
# - fake-clock timeout (~60s virtual)
# - successful response after deadline rejected
# No VPS / no real install.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL="${ROOT}/ops/vps/INSTALL_ON_VPS.sh"
DELAY_SEC="${HEALTHZ_TEST_DELAY_SEC:-3}"
MAX_WAIT=60

fail() { echo "FAIL: $*"; exit 1; }
pass() { echo "PASS: $*"; }

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
grep -q '_asa_healthz_now_ns' "$INSTALL" || fail "ns clock hook missing"
grep -q 'monotonic_ns' "$INSTALL" || fail "monotonic_ns preferred clock missing"
grep -q 'PRECISION LIMITED' "$INSTALL" || fail "fallback precision limitation not documented"
grep -q '_asa_healthz_ns_to_curl_s' "$INSTALL" || fail "curl budget formatter missing"
grep -q 'Truncates DOWN' "$INSTALL" || fail "truncate-down documentation missing"

fn_body="$(awk '
  /^wait_for_local_healthz\(\)/ {on=1}
  on {print}
  /^report_service_smoke_diagnostics\(\)/ {if(on){exit}}
' "$INSTALL")"
fn_body="$(printf '%s\n' "$fn_body" | sed '/^report_service_smoke_diagnostics/,$d')"
if printf '%s\n' "$fn_body" | grep -E 'while\s+\[\s*"\$i"\s+-lt' | grep -q .; then
  fail "iteration-count while-loop still present"
fi
printf '%s\n' "$fn_body" | grep -q 'deadline_ns=' || fail "deadline_ns missing"
if printf '%s\n' "$fn_body" | grep -E 'healthz OK' -A8 | grep -E 'curl\s+-fsS' | grep -q .; then
  fail "second curl after success still present"
fi
printf '%s\n' "$fn_body" | grep -q 'responded after' || fail "post-success deadline check missing"
smoke_region="$(awk '/sudo systemctl restart asa-physio.service/,/verify effective SMS/' "$INSTALL")"
printf '%s\n' "$smoke_region" | grep -q 'wait_for_local_healthz' || fail "smoke path missing wait_for_local_healthz"
if printf '%s\n' "$smoke_region" | grep -E '^\s*sleep 2\s*$' | grep -q .; then
  fail "smoke path still has bare sleep 2"
fi
pass "static: monotonic ns deadline + single-curl success + helpers wired"

extract="$(awk '
  /^_asa_healthz_now_ns\(\)/ {on=1}
  on {print}
  /^report_service_smoke_diagnostics\(\)/ {if(on){exit}}
' "$INSTALL")"
extract="$(printf '%s\n' "$extract" | sed '/^report_service_smoke_diagnostics/,$d')"
[ -n "$extract" ] || fail "could not extract wait_for_local_healthz"
eval "$extract"

command -v curl >/dev/null || fail "curl required"
command -v awk >/dev/null || fail "awk required"

# --- Unit: formatter truncates down; never exceeds input ns ---
assert_fmt_le() {
  local ns="$1"
  local out out_ns
  out="$(_asa_healthz_ns_to_curl_s "$ns")"
  out_ns="$(awk -v s="$out" 'BEGIN{printf "%.0f", s * 1e9}')"
  if [ "$out_ns" -gt "$ns" ]; then
    fail "formatter($ns) -> $out (${out_ns}ns) exceeds input"
  fi
}
assert_fmt_le 0
assert_fmt_le 999999
out999="$(_asa_healthz_ns_to_curl_s 999999)"
[ "$out999" = "0.000" ] || fail "sub-1ms budget must be 0.000, got $out999"
assert_fmt_le 1000000
out1ms="$(_asa_healthz_ns_to_curl_s 1000000)"
[ "$out1ms" = "0.001" ] || fail "exactly 1ms must be 0.001, got $out1ms"
# Classic round-up trap: 1.5ms must become 0.001 not 0.002
assert_fmt_le 1500000
out15="$(_asa_healthz_ns_to_curl_s 1500000)"
[ "$out15" = "0.001" ] || fail "1.5ms must truncate to 0.001, got $out15"
assert_fmt_le 1999999
out19="$(_asa_healthz_ns_to_curl_s 1999999)"
[ "$out19" = "0.001" ] || fail "1.999ms must truncate to 0.001, got $out19"
assert_fmt_le 1000000000
assert_fmt_le 1000000001
pass "unit: _asa_healthz_ns_to_curl_s truncates down and never exceeds input"

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

t_start="$(_asa_healthz_now_ns)"
if ! wait_for_local_healthz "http://127.0.0.1:${PORT}/api/healthz" "$MAX_WAIT"; then
  fail "wait_for_local_healthz timed out (should succeed before ${MAX_WAIT}s)"
fi
t_end="$(_asa_healthz_now_ns)"
elapsed_ns=$((t_end - t_start))
elapsed_s="$(awk -v ns="$elapsed_ns" 'BEGIN{printf "%.0f", ns/1e9}')"
if [ "$elapsed_s" -lt $((DELAY_SEC - 1)) ]; then
  fail "succeeded too fast (${elapsed_s}s < delay ${DELAY_SEC}s)"
fi
if [ "$elapsed_s" -ge "$MAX_WAIT" ]; then
  fail "elapsed ${elapsed_s}s reached max — expected success earlier"
fi
pass "runtime: healthz OK after delayed startup (elapsed≈${elapsed_s}s, delay=${DELAY_SEC}s, max=${MAX_WAIT}s)"

cleanup
trap - EXIT

# --- Runtime B: timeout via fake ns clock (no real 60s wait) ---
FAKE_NOW_NS=1000000000000000
FAKE_SLEEPS=0
_asa_healthz_now_ns() { echo "$FAKE_NOW_NS"; }
_asa_healthz_sleep() {
  local sec="${1:-1}"
  local add_ns
  add_ns="$(awk -v s="$sec" 'BEGIN{printf "%.0f", s*1e9}')"
  FAKE_NOW_NS=$((FAKE_NOW_NS + add_ns))
  FAKE_SLEEPS=$((FAKE_SLEEPS + 1))
}
curl() { return 7; }

wall_a="$(date +%s)"
set +e
wait_for_local_healthz "http://127.0.0.1:9/api/healthz" 60
rc=$?
set -e
wall_b="$(date +%s)"
wall_elapsed=$((wall_b - wall_a))
fake_elapsed_s="$(awk -v a=1000000000000000 -v b="$FAKE_NOW_NS" 'BEGIN{printf "%.0f", (b-a)/1e9}')"

[ "$rc" -eq 1 ] || fail "expected timeout rc=1, got $rc"
if [ "$fake_elapsed_s" -lt 59 ] || [ "$fake_elapsed_s" -gt 60 ]; then
  fail "fake elapsed=${fake_elapsed_s}s — expected ~60s wall-clock deadline"
fi
if [ "$wall_elapsed" -ge 15 ]; then
  fail "wall elapsed ${wall_elapsed}s too long — fake clock/curl not used"
fi
pass "runtime: timeout respects 60s ns deadline via fake clock (fake=${fake_elapsed_s}s wall=${wall_elapsed}s sleeps=${FAKE_SLEEPS})"

# --- Runtime C: success after deadline must be rejected ---
FAKE_NOW_NS=0
_asa_healthz_now_ns() { echo "$FAKE_NOW_NS"; }
_asa_healthz_sleep() { fail "sleep should not run on immediate post-deadline success path"; }
curl() {
  # Simulate slow response that only "finishes" after the 60s deadline.
  FAKE_NOW_NS=$((60 * 1000000000 + 1))
  printf '%s\n' '{"ok":true}'
  return 0
}
set +e
out="$(wait_for_local_healthz "http://127.0.0.1:9/api/healthz" 60 2>&1)"
rc=$?
set -e
[ "$rc" -eq 1 ] || fail "post-deadline success must return 1, got $rc"
printf '%s\n' "$out" | grep -q 'responded after' || fail "expected responded-after-deadline message"
printf '%s\n' "$out" | grep -q 'healthz OK' && fail "must not claim healthz OK after deadline"
pass "runtime: successful response after deadline is rejected"

grep -q 'already present' "$INSTALL" || fail "owner skip path missing"
grep -q 'credentials unchanged' "$INSTALL" || fail "owner no-overwrite wording missing"
grep -q 'REMINDER_BACKGROUND=0' "$INSTALL" || fail "REMINDER_BACKGROUND=0 missing"
grep -q '127.0.0.1:9/asa-sms-sink' "$INSTALL" || fail "SMS sink missing"
pass "static: owner re-run skip + SMS/reminder sinks preserved"

echo "ALL: PASS"
