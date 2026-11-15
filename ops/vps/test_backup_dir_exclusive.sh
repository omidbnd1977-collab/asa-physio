#!/usr/bin/env bash
# Regression: create_exclusive_run_backup_dir refuses an existing path (no reuse/mix).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL="${ROOT}/ops/vps/INSTALL_ON_VPS.sh"

fail() { echo "FAIL: $*"; exit 1; }
pass() { echo "PASS: $*"; }

bash -n "$INSTALL" || fail "bash -n INSTALL"
pass "bash -n"

grep -q 'create_exclusive_run_backup_dir' "$INSTALL" || fail "helper missing"
grep -q 'create_exclusive_run_backup_dir "\$RUN_BACKUP_DIR"' "$INSTALL" \
  || fail "RUN_BACKUP_DIR must use exclusive create"
# Leaf create must not use mkdir -p inside the exclusive helper.
fn="$(awk '
  /^create_exclusive_run_backup_dir\(\)/ {on=1}
  on {print}
  /^set_install_phase\(\)/ {if(on) exit}
' "$INSTALL")"
fn="$(printf '%s\n' "$fn" | sed '/^set_install_phase/,$d')"
if printf '%s\n' "$fn" | grep -E 'mkdir[[:space:]]+-p' | grep -q .; then
  fail "exclusive helper must not mkdir -p the leaf"
fi
printf '%s\n' "$fn" | grep -q 'sudo mkdir "\$d"' || fail "exclusive helper must mkdir leaf without -p"
pass "static: exclusive backup dir wiring"

# Extract helper (mock sudo as identity for local FS).
extract="$(awk '
  /^create_exclusive_run_backup_dir\(\)/ {on=1}
  on {print}
  /^set_install_phase\(\)/ {if(on) exit}
' "$INSTALL")"
extract="$(printf '%s\n' "$extract" | sed '/^set_install_phase/,$d')"
sudo() { "$@"; }
chown() { :; }
chmod() { :; }
eval "$extract"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- New directory succeeds ---
NEW="${TMP}/bootstrap-new"
create_exclusive_run_backup_dir "$NEW"
[ -d "$NEW" ] || fail "new dir not created"
# mode best-effort (may be no-op on some Windows mounts)
pass "runtime: creates new exclusive backup dir"

# --- Collision: existing dir must abort without changing contents ---
EXIST="${TMP}/bootstrap-existing"
mkdir -p "$EXIST"
echo "keep-me" >"${EXIST}/marker.txt"
marker_before="$(cat "${EXIST}/marker.txt")"
ls_before="$(ls -1A "$EXIST" | sort | tr '\n' ' ')"

set +e
out="$(create_exclusive_run_backup_dir "$EXIST" 2>&1)"
rc=$?
set -e
[ "$rc" -ne 0 ] || fail "expected non-zero exit on collision"
printf '%s\n' "$out" | grep -qi 'already exists' || fail "expected clear already-exists error"
marker_after="$(cat "${EXIST}/marker.txt")"
ls_after="$(ls -1A "$EXIST" | sort | tr '\n' ' ')"
[ "$marker_before" = "$marker_after" ] || fail "marker content changed on collision"
[ "$ls_before" = "$ls_after" ] || fail "directory listing changed on collision"
pass "runtime: collision aborts without modifying existing backup dir"

# --- Collision: existing file at path also refused ---
FILEPATH="${TMP}/bootstrap-as-file"
echo x >"$FILEPATH"
set +e
out2="$(create_exclusive_run_backup_dir "$FILEPATH" 2>&1)"
rc2=$?
set -e
[ "$rc2" -ne 0 ] || fail "expected non-zero when path is an existing file"
[ -f "$FILEPATH" ] || fail "file path must remain"
pass "runtime: existing file path refused"

echo "ALL: PASS"
