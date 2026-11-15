#!/usr/bin/env bash
# Regression: RESTORE.txt lives in root-only bootstrap (700/600). Validations must
# use `sudo grep`, not bare grep (false FATAL on asaadmin). Does not touch a VPS.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL="${ROOT}/ops/vps/INSTALL_ON_VPS.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*"; exit 1; }
pass() { echo "PASS: $*"; }

command -v bash >/dev/null || fail "bash missing"
bash -n "$INSTALL" || fail "bash -n INSTALL_ON_VPS.sh"
pass "bash -n"

# Static: inside write_restore_notes, every grep on RESTORE_NOTES must be sudo grep.
# Extract function body roughly between write_restore_notes() { and ensure_certbot.
body="$(awk '
  /^write_restore_notes\(\)/ {on=1}
  on {print}
  /^ensure_certbot_timer_and_dry_run\(\)/ {if(on){exit}}
' "$INSTALL")"
[ -n "$body" ] || fail "could not extract write_restore_notes"

# Bare grep targeting RESTORE_NOTES (bug pattern).
if printf '%s\n' "$body" | grep -E '(^|[^[:alnum:]_])grep[[:space:]]' | grep -F 'RESTORE_NOTES' | grep -v 'sudo grep' | grep -q .; then
  printf '%s\n' "$body" | grep -E '(^|[^[:alnum:]_])grep[[:space:]]' | grep -F 'RESTORE_NOTES' | grep -v 'sudo grep' || true
  fail "bare grep on RESTORE_NOTES still present"
fi
pass "static: no bare grep on RESTORE_NOTES"

if ! printf '%s\n' "$body" | grep -q 'sudo grep -Fq'; then
  fail "expected sudo grep -Fq in write_restore_notes"
fi
pass "static: sudo grep -Fq present"

# protect_dir still root-only (not world-readable).
grep -q 'sudo chmod 700' "$INSTALL" || fail "protect_dir chmod 700 missing"
grep -q 'sudo chown root:root' "$INSTALL" || fail "protect_dir chown root missing"
pass "static: bootstrap stays root-only (700)"

# Runtime simulation: root-only RESTORE; operator grep fails; sudo-grep mock succeeds.
# Avoid mkdir -m (fails on some Windows/Git Bash /tmp mounts); chmod best-effort after.
BOOT="${TMP}/bootstrap-root"
mkdir -p "$BOOT"
chmod 700 "$BOOT" 2>/dev/null || true
RESTORE_NOTES="${BOOT}/RESTORE.txt"
DB_FILE="/opt/asa-physio/data/asa.db"
UPLOAD_DIR_VALIDATED="/opt/asa-physio/data/uploads"
# Content mirrors required validation needles (not a full RESTORE.txt).
cat >"$RESTORE_NOTES" <<EOF
RUN_BACKUP_DIR=${BOOT}
DB_FILE=${DB_FILE}
UPLOAD_DIR=${UPLOAD_DIR_VALIDATED}
includes UPLOAD_DIR=${UPLOAD_DIR_VALIDATED}
${DB_FILE}-wal
${DB_FILE}-shm
asa.db.pre-migrate ${DB_FILE}
quarantine-tree-before-restore
asa-physio.service.enabled.state
asa-physio.service.active.state
certbot.timer.enabled.state
NEVER start only because the unit file exists
do not start merely because unit exists
MARKER: overrides-before-service-start
MARKER: service-start-after-overrides
rebuild .venv
python3 -m venv
EOF
chmod 600 "$RESTORE_NOTES" 2>/dev/null || true

# Hide from non-sudo readers by moving bytes into a root store; operator path emptied.
# Real content only reachable via fake sudo (models root-only bootstrap without chmod 000,
# which is unreliable on Windows/NTFS).
ROOT_STORE="${TMP}/.root_store"
mkdir -p "$ROOT_STORE"
chmod 700 "$ROOT_STORE" 2>/dev/null || true
cp -a "$RESTORE_NOTES" "${ROOT_STORE}/RESTORE.txt"
: >"$RESTORE_NOTES"
chmod 600 "$RESTORE_NOTES" 2>/dev/null || true

# Bare grep against operator path must not see content (file empty / unreadable).
if grep -Fq "DB_FILE=${DB_FILE}" "$RESTORE_NOTES" 2>/dev/null; then
  fail "operator bare grep unexpectedly matched (simulation broken)"
fi
pass "runtime: operator bare grep cannot validate root-only RESTORE"

# Fake sudo: only elevates grep/tee to the root store copy (keeps bootstrap non-public).
sudo() {
  local cmd="${1:-}"
  shift || true
  case "$cmd" in
    grep)
      # Rewrite last arg if it points at RESTORE_NOTES path → root store
      local args=("$@")
      local i
      for i in "${!args[@]}"; do
        if [ "${args[$i]}" = "$RESTORE_NOTES" ]; then
          args[$i]="${ROOT_STORE}/RESTORE.txt"
        fi
      done
      command grep "${args[@]}"
      ;;
    *)
      fail "unexpected sudo cmd in test: $cmd"
      ;;
  esac
}

if ! sudo grep -Fq -- "DB_FILE=${DB_FILE}" "$RESTORE_NOTES"; then
  fail "sudo grep mock should find DB_FILE"
fi
if ! sudo grep -Fq -- "${DB_FILE}-wal" "$RESTORE_NOTES"; then
  fail "sudo grep mock should find wal path"
fi
_ov_line="$(sudo grep -n -- 'MARKER: overrides-before-service-start' "$RESTORE_NOTES" | head -n1 | cut -d: -f1 || true)"
_st_line="$(sudo grep -n -- 'MARKER: service-start-after-overrides' "$RESTORE_NOTES" | head -n1 | cut -d: -f1 || true)"
[ -n "${_ov_line:-}" ] && [ -n "${_st_line:-}" ] && [ "$_ov_line" -lt "$_st_line" ] \
  || fail "marker order via sudo grep"
pass "runtime: sudo grep validates root-only RESTORE without opening dir to world"

# Ensure we did not chmod bootstrap to 755/644 in the installer helpers.
if grep -E 'protect_dir|protect_file' -A6 "$INSTALL" | grep -E 'chmod 755|chmod 644|chmod 777'; then
  fail "protect_* must not loosen to world-readable modes"
fi
pass "static: protect_* modes stay restrictive"

echo "ALL: PASS"
