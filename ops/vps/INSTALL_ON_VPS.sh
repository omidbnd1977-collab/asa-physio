#!/usr/bin/env bash
# Self-contained installer — does NOT require ops/vps inside the git tree.
# Copy this file to the VPS, then:
#   chmod +x INSTALL_ON_VPS.sh && ./INSTALL_ON_VPS.sh
#
# HTTPS email policy (pick ONE at preflight — BEFORE any server mutation):
#   export CERTBOT_EMAIL='you@example.com'     # required for Let's Encrypt
#   OR
#   export CERTBOT_SKIP_HTTPS=1                # explicit: HTTP only, no certbot
#   OR
#   export CERTBOT_NO_EMAIL=1                  # explicit: certbot --register-unsafely-without-email
#
# Does NOT: force-push, overwrite .env.production, send real SMS, change DNS,
# or print secrets (including admin passwords). Forward-only migrations. One uvicorn worker.
# Initial owner: if no role=owner exists after migrate, requires interactive (or env)
# ADMIN_USER/ADMIN_PASSWORD and runs `python3 -m ops.seed` BEFORE systemd start.
#
# Password channel limitation (ops.seed): ADMIN_PASSWORD is read only from the
# process environment — stdin/FD is NOT supported without changing application
# code. Absolute secrecy of the password in RAM/environ is NOT claimed.
#
# HTTPS: unless CERTBOT_SKIP_HTTPS=1, failed DNS/certbot/HTTPS probe ends the
# installer with non-zero status (partial install; success is NOT claimed).
# DB is never auto-rolled-back after migrate (migration compatibility risk).
set -euo pipefail

TARGET_SHA="9987b78de68c383c14206fbc6274923a7a604f44"
APP_ROOT="/opt/asa-physio"
BACKUP_ROOT="/opt/asa-physio-backups"
BACKUP_TAR="${BACKUP_ROOT}/asa-before-update-20261113-194450.tar.gz"
BACKUP_SHA256="857f0026f7ebbf6eb03096fea14dc925cffdb45f09d1c79d01d13ce1e37d4cbe"
TS="$(date -u +%Y%m%d-%H%M%S)"
RUN_BACKUP_DIR="${BACKUP_ROOT}/bootstrap-${TS}"
OVERRIDE_DIR="/etc/asa-physio"
OVERRIDE_ENV="${OVERRIDE_DIR}/runtime-overrides.env"
UNIT_PATH="/etc/systemd/system/asa-physio.service"
NGINX_AVAILABLE="/etc/nginx/sites-available/asa-physio"
NGINX_ENABLED="/etc/nginx/sites-enabled/asa-physio"
NGINX_DEFAULT_ENABLED="/etc/nginx/sites-enabled/default"
# Set in preflight from .env.production DB_PATH (validated under APP_ROOT). Never print .env secrets.
DB_FILE=""
HTTPS_OK=0
CERTBOT_TIMER_OK=0
CERTBOT_RENEW_DRY_RUN_OK=0
# yes|no — whether any role=owner existed before install mutations (DB probe).
OWNER_EXISTED_BEFORE=""
RESTORE_NOTES="${RUN_BACKUP_DIR}/RESTORE.txt"
DB_STATE_FILE="${RUN_BACKUP_DIR}/asa.db.state"
INCOMPLETE_MARKER="${RUN_BACKUP_DIR}/INSTALL_INCOMPLETE"
COMPLETE_MARKER="${RUN_BACKUP_DIR}/INSTALL_COMPLETE"
PHASE_FILE="${RUN_BACKUP_DIR}/install.phase"
# preflight → backups_verified → mutating → migrated → seeded → service → nginx → certbot → done
INSTALL_PHASE="preflight"
# success | partial_http_only | failed_https | failed_smoke | failed_other
INSTALL_OUTCOME="failed_other"
NON_DB_ROLLBACK_ATTEMPTED=0
NON_DB_ROLLBACK_VERIFIED=0
CERTBOT_TIMER_RESTORE_ATTEMPTED=0
CERTBOT_TIMER_RESTORE_VERIFIED=0
# Guard so fail_partial_install / on_err never double-run rollback or hints.
HANDLING_FAILURE=0
# Validated under APP_ROOT (like DB_FILE). Set in preflight; forced after sourcing .env.
UPLOAD_DIR_VALIDATED=""

umask 077

# Read ONLY DB_PATH from .env.production (no full source; no secret dump).
# Default if unset: <APP_ROOT>/data/asa.db. Must resolve under APP_ROOT or abort.
resolve_db_path_from_env() {
  local envf="$APP_ROOT/.env.production"
  # Python prints a path *relative to APP_ROOT*; bash joins with APP_ROOT so the
  # result always matches the shell's APP_ROOT string (important under Git Bash).
  _db_rel="$(
    APP_ROOT="$APP_ROOT" ENVF="$envf" python3 - <<'PY'
import os, re, sys
from pathlib import Path

app = Path(os.environ["APP_ROOT"]).resolve()
envf = Path(os.environ["ENVF"])
if not envf.is_file():
    print("FATAL: .env.production missing while resolving DB_PATH", file=sys.stderr)
    sys.exit(2)

raw = None
for line in envf.read_text(encoding="utf-8", errors="replace").splitlines():
    s = line.strip()
    if not s or s.startswith("#"):
        continue
    if s.startswith("export "):
        s = s[7:].strip()
    if re.match(r"^DB_PATH\s*=", s):
        raw = s.split("=", 1)[1].strip()
        if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
            raw = raw[1:-1]
        break

# Key present but empty → hard stop (do not silently fall back to default).
if raw is not None and raw == "":
    print(
        "FATAL: DB_PATH is present in .env.production but empty — refusing to continue",
        file=sys.stderr,
    )
    sys.exit(6)

if raw is None:
    candidate = (app / "data" / "asa.db").resolve()
else:
    p = Path(raw)
    candidate = p.resolve() if p.is_absolute() else (app / p).resolve()

try:
    rel = candidate.relative_to(app)
except ValueError:
    print(
        f"FATAL: DB_PATH resolves outside APP_ROOT ({app}): refused",
        file=sys.stderr,
    )
    sys.exit(3)

if candidate.exists() and not candidate.is_file():
    print("FATAL: DB_PATH exists but is not a regular file", file=sys.stderr)
    sys.exit(4)

try:
    candidate.parent.resolve().relative_to(app)
except ValueError:
    print("FATAL: DB_PATH parent outside APP_ROOT", file=sys.stderr)
    sys.exit(5)

# Relative posix path only (no drive letters / symlink canonical roots).
print(rel.as_posix())
PY
  )" || {
    echo "FATAL: could not extract/validate DB_PATH from .env.production — no changes made"
    return 1
  }
  if [ -z "${_db_rel:-}" ] || [[ "$_db_rel" == /* ]] || [[ "$_db_rel" == *..* ]]; then
    echo "FATAL: invalid relative DB_PATH from resolver — no changes made"
    return 1
  fi
  DB_FILE="${APP_ROOT%/}/${_db_rel}"
  case "$DB_FILE" in
    "$APP_ROOT"/*) ;;
    *)
      echo "FATAL: DB_FILE not under APP_ROOT after resolve — no changes made"
      return 1
      ;;
  esac
  echo "DB_FILE validated: $DB_FILE"
}

# Read ONLY UPLOAD_DIR from .env.production (no secret dump). Must stay under APP_ROOT.
# Empty key → hard stop. Unset → default <APP_ROOT>/data/uploads.
resolve_upload_dir_from_env() {
  local envf="$APP_ROOT/.env.production"
  _up_rel="$(
    APP_ROOT="$APP_ROOT" ENVF="$envf" python3 - <<'PY'
import os, re, sys
from pathlib import Path

app = Path(os.environ["APP_ROOT"]).resolve()
envf = Path(os.environ["ENVF"])
if not envf.is_file():
    print("FATAL: .env.production missing while resolving UPLOAD_DIR", file=sys.stderr)
    sys.exit(2)

raw = None
for line in envf.read_text(encoding="utf-8", errors="replace").splitlines():
    s = line.strip()
    if not s or s.startswith("#"):
        continue
    if s.startswith("export "):
        s = s[7:].strip()
    if re.match(r"^UPLOAD_DIR\s*=", s):
        raw = s.split("=", 1)[1].strip()
        if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
            raw = raw[1:-1]
        break

if raw is not None and raw == "":
    print(
        "FATAL: UPLOAD_DIR is present in .env.production but empty — refusing to continue",
        file=sys.stderr,
    )
    sys.exit(6)

if raw is None:
    candidate = (app / "data" / "uploads").resolve()
else:
    p = Path(raw)
    candidate = p.resolve() if p.is_absolute() else (app / p).resolve()

try:
    rel = candidate.relative_to(app)
except ValueError:
    print(
        f"FATAL: UPLOAD_DIR resolves outside APP_ROOT ({app}): refused",
        file=sys.stderr,
    )
    sys.exit(3)

if candidate.exists() and not candidate.is_dir():
    print("FATAL: UPLOAD_DIR exists but is not a directory", file=sys.stderr)
    sys.exit(4)

print(rel.as_posix())
PY
  )" || {
    echo "FATAL: could not extract/validate UPLOAD_DIR from .env.production — no changes made"
    return 1
  }
  if [ -z "${_up_rel:-}" ] || [[ "$_up_rel" == /* ]] || [[ "$_up_rel" == *..* ]]; then
    echo "FATAL: invalid relative UPLOAD_DIR from resolver — no changes made"
    return 1
  fi
  UPLOAD_DIR_VALIDATED="${APP_ROOT%/}/${_up_rel}"
  case "$UPLOAD_DIR_VALIDATED" in
    "$APP_ROOT"/*) ;;
    *)
      echo "FATAL: UPLOAD_DIR not under APP_ROOT after resolve — no changes made"
      return 1
      ;;
  esac
  echo "UPLOAD_DIR validated under APP_ROOT: $UPLOAD_DIR_VALIDATED"
}

protect_file() {
  local f="$1"
  [ -e "$f" ] || return 0
  sudo chown root:root "$f"
  sudo chmod 600 "$f"
}

protect_dir() {
  local d="$1"
  sudo mkdir -p "$d"
  sudo chown root:root "$d"
  sudo chmod 700 "$d"
}

set_install_phase() {
  INSTALL_PHASE="$1"
  if sudo test -d "$RUN_BACKUP_DIR" 2>/dev/null; then
    printf '%s\n' "$INSTALL_PHASE" | sudo tee "$PHASE_FILE" >/dev/null
    protect_file "$PHASE_FILE" || true
  fi
  echo "install phase: $INSTALL_PHASE"
}

mark_install_incomplete() {
  printf 'incomplete phase=%s ts=%s\n' "$INSTALL_PHASE" "$TS" | sudo tee "$INCOMPLETE_MARKER" >/dev/null
  protect_file "$INCOMPLETE_MARKER"
}

mark_install_complete() {
  sudo rm -f "$INCOMPLETE_MARKER"
  printf 'complete outcome=%s ts=%s\n' "$INSTALL_OUTCOME" "$TS" | sudo tee "$COMPLETE_MARKER" >/dev/null
  protect_file "$COMPLETE_MARKER"
}

# Write SHA-256 sidecar and verify immediately. Does not print file contents.
sha256_record_and_verify() {
  local f="$1"
  local sumfile="${f}.sha256"
  if ! sudo test -f "$f"; then
    echo "FATAL: cannot checksum missing file: $f"
    exit 1
  fi
  sudo sha256sum "$f" | sudo tee "$sumfile" >/dev/null
  protect_file "$sumfile"
  if ! sudo sha256sum -c "$sumfile"; then
    echo "FATAL: SHA-256 verification failed for $f — no further mutations"
    exit 1
  fi
  echo "SHA-256 OK: $f (sidecar ${sumfile}; digest not echoed again)"
}

# Verify an existing sidecar without rewriting it.
sha256_verify_existing() {
  local f="$1"
  local sumfile="${f}.sha256"
  if ! sudo test -f "$f" || ! sudo test -f "$sumfile"; then
    echo "FATAL: missing file or .sha256 sidecar: $f"
    exit 1
  fi
  sudo sha256sum -c "$sumfile"
  echo "SHA-256 re-check OK: $f"
}

warn_previous_incomplete_runs() {
  local d
  local found=0
  # Re-run must NOT delete or alter other bootstrap-* directories.
  shopt -s nullglob
  for d in "${BACKUP_ROOT}"/bootstrap-*; do
    [ -d "$d" ] || continue
    [ "$d" = "$RUN_BACKUP_DIR" ] && continue
    if sudo test -f "${d}/INSTALL_INCOMPLETE"; then
      echo "NOTE: previous incomplete install left intact: $d"
      found=1
    fi
  done
  shopt -u nullglob
  if [ "$found" = 0 ]; then
    echo "No previous INSTALL_INCOMPLETE markers found (or none readable)."
  fi
  echo "This run backup dir (new, not overwriting others): $RUN_BACKUP_DIR"
}

print_manual_restore_and_continue_hints() {
  echo "----- SAFE NEXT STEPS (operator) -----"
  echo "Partial/failed install — SUCCESS IS NOT CLAIMED."
  echo "Backups for THIS run: ${RUN_BACKUP_DIR}"
  if sudo test -f "$RESTORE_NOTES"; then
    echo "Manual FULL restore procedure: ${RESTORE_NOTES}"
    echo "  (paths only — do not cat env.production.bak)"
  else
    echo "RESTORE.txt not available yet (failure before backups completed)."
  fi
  echo "DB was NOT auto-rolled-back after migrate (migration compatibility risk)."
  echo "To continue later: fix DNS/cert/policy, then re-run this script"
  echo "  (creates a NEW bootstrap-* dir; previous backups stay intact)."
  echo "-------------------------------------"
}

# Restore nginx / systemd unit / enable-state / overrides only.
# NEVER touches DB or APP_ROOT tree. NEVER claims rollback success unless verified.
safe_non_db_rollback_attempt() {
  local ok=1
  NON_DB_ROLLBACK_ATTEMPTED=1
  NON_DB_ROLLBACK_VERIFIED=0
  echo "=== safe non-DB rollback attempt (DB/tree NOT touched) ==="

  if ! sudo test -d "$RUN_BACKUP_DIR"; then
    echo "No RUN_BACKUP_DIR — nothing to roll back non-DB."
    return 0
  fi

  # Stop first so a later start reloads EnvironmentFile from restored overrides.
  sudo systemctl stop asa-physio.service 2>/dev/null || true

  # 1) systemd unit file
  if sudo test -f "${RUN_BACKUP_DIR}/asa-physio.service.prev"; then
    sudo cp -a "${RUN_BACKUP_DIR}/asa-physio.service.prev" "$UNIT_PATH" || ok=0
  elif sudo test -f "${RUN_BACKUP_DIR}/asa-physio.service.prev.missing"; then
    sudo rm -f "$UNIT_PATH" || ok=0
  fi

  # 2) runtime-overrides.env BEFORE any service start (EnvironmentFile must be in place)
  # MARKER: overrides-before-service-start
  if sudo test -f "${RUN_BACKUP_DIR}/runtime-overrides.env.prev"; then
    sudo mkdir -p "$OVERRIDE_DIR"
    sudo cp -a "${RUN_BACKUP_DIR}/runtime-overrides.env.prev" "$OVERRIDE_ENV" || ok=0
    sudo chmod 600 "$OVERRIDE_ENV" || true
  elif sudo test -f "${RUN_BACKUP_DIR}/runtime-overrides.env.prev.missing"; then
    sudo rm -f "$OVERRIDE_ENV" || true
  fi

  # 3) reload unit definitions after unit + overrides are on disk
  sudo systemctl daemon-reload || ok=0

  # 4) enabled/disabled (does not start the service)
  if sudo test -f "${RUN_BACKUP_DIR}/asa-physio.service.enabled.state"; then
    _en="$(sudo cat "${RUN_BACKUP_DIR}/asa-physio.service.enabled.state" 2>/dev/null || echo missing)"
    case "$_en" in
      enabled|enabled-runtime)
        if [ -f "$UNIT_PATH" ]; then sudo systemctl enable asa-physio.service || ok=0; fi
        ;;
      disabled)
        sudo systemctl disable asa-physio.service 2>/dev/null || true
        ;;
      *)
        sudo systemctl disable asa-physio.service 2>/dev/null || true
        sudo rm -f /etc/systemd/system/multi-user.target.wants/asa-physio.service || true
        ;;
    esac
  fi

  # 5) active/inactive ONLY after overrides restored (do NOT start merely because unit exists).
  # MARKER: service-start-after-overrides
  if sudo test -f "${RUN_BACKUP_DIR}/asa-physio.service.active.state"; then
    _act="$(sudo cat "${RUN_BACKUP_DIR}/asa-physio.service.active.state" 2>/dev/null || echo inactive)"
    case "$_act" in
      active|activating|reloading)
        if [ -f "$UNIT_PATH" ]; then
          sudo systemctl start asa-physio.service || ok=0
        else
          echo "WARN: prior active=$_act but unit missing after rollback — cannot start"
          ok=0
        fi
        ;;
      *)
        sudo systemctl stop asa-physio.service 2>/dev/null || true
        ;;
    esac
  else
    sudo systemctl stop asa-physio.service 2>/dev/null || true
  fi

  # 6) certbot.timer prior enabled/active (separate verification; independent of app start order)
  restore_certbot_timer_prior_state || ok=0

  # nginx available
  if sudo test -f "${RUN_BACKUP_DIR}/nginx-asa-physio.prev"; then
    sudo cp -a "${RUN_BACKUP_DIR}/nginx-asa-physio.prev" "$NGINX_AVAILABLE" || ok=0
  elif sudo test -f "${RUN_BACKUP_DIR}/nginx-asa-physio.prev.missing"; then
    sudo rm -f "$NGINX_AVAILABLE" || true
  fi

  # nginx enabled asa-physio
  if sudo test -f "${RUN_BACKUP_DIR}/nginx-asa-physio.enabled.kind"; then
    _kind="$(sudo cat "${RUN_BACKUP_DIR}/nginx-asa-physio.enabled.kind" 2>/dev/null || echo missing)"
    case "$_kind" in
      symlink)
        _link="$(sudo cat "${RUN_BACKUP_DIR}/nginx-asa-physio.enabled.link")"
        sudo ln -sfn "$_link" "$NGINX_ENABLED" || ok=0
        ;;
      file)
        sudo cp -a "${RUN_BACKUP_DIR}/nginx-asa-physio.enabled.prev" "$NGINX_ENABLED" || ok=0
        ;;
      *)
        sudo rm -f "$NGINX_ENABLED" || true
        ;;
    esac
  fi

  # nginx default enabled
  if sudo test -f "${RUN_BACKUP_DIR}/nginx-default.enabled.kind"; then
    _kind="$(sudo cat "${RUN_BACKUP_DIR}/nginx-default.enabled.kind" 2>/dev/null || echo missing)"
    case "$_kind" in
      symlink)
        _link="$(sudo cat "${RUN_BACKUP_DIR}/nginx-default.enabled.link")"
        sudo ln -sfn "$_link" "$NGINX_DEFAULT_ENABLED" || ok=0
        ;;
      file)
        sudo cp -a "${RUN_BACKUP_DIR}/nginx-default.enabled.prev" "$NGINX_DEFAULT_ENABLED" || ok=0
        ;;
      *)
        sudo rm -f "$NGINX_DEFAULT_ENABLED" || true
        ;;
    esac
  fi

  if sudo nginx -t 2>/dev/null; then
    sudo systemctl reload nginx || ok=0
  else
    echo "WARN: nginx -t failed after non-DB rollback attempt — operator must fix."
    ok=0
  fi

  if [ "$ok" = 1 ]; then
    NON_DB_ROLLBACK_VERIFIED=1
    echo "NON_DB_ROLLBACK: files restored and nginx -t OK (NOT a full install rollback; DB/tree untouched)."
  else
    NON_DB_ROLLBACK_VERIFIED=0
    echo "NON_DB_ROLLBACK: attempted but NOT verified successful. Do NOT treat as rolled back."
  fi
  echo "Full DB/tree restore remains MANUAL via RESTORE.txt — success of that path is NOT claimed here."
}

backup_enabled_path() {
  local live="$1"
  local prefix="$2"
  local kind_file="${RUN_BACKUP_DIR}/${prefix}.kind"
  local link_file="${RUN_BACKUP_DIR}/${prefix}.link"
  local copy_file="${RUN_BACKUP_DIR}/${prefix}.prev"

  if [ -L "$live" ]; then
    printf '%s' "symlink" | sudo tee "$kind_file" >/dev/null
    readlink "$live" | sudo tee "$link_file" >/dev/null
    protect_file "$kind_file"
    protect_file "$link_file"
    echo "${prefix}: symlink (link text saved; not printed)"
  elif [ -e "$live" ]; then
    printf '%s' "file" | sudo tee "$kind_file" >/dev/null
    sudo cp -a "$live" "$copy_file"
    protect_file "$kind_file"
    protect_file "$copy_file"
    echo "${prefix}: regular file/dir copied"
  else
    printf '%s' "missing" | sudo tee "$kind_file" >/dev/null
    protect_file "$kind_file"
    echo "${prefix}: was missing"
  fi
}

# Consistent SQLite snapshot via Backup API (safe with WAL / live readers).
sqlite_backup_api() {
  local src="$1"
  local dst="$2"
  sudo python3 - "$src" "$dst" <<'PY'
import sys, sqlite3, os
src_path, dst_path = sys.argv[1], sys.argv[2]
if not os.path.isfile(src_path):
    raise SystemExit(f"source db missing: {src_path}")
# Remove incomplete destination if present
if os.path.exists(dst_path):
    os.remove(dst_path)
src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=60.0)
try:
    dst = sqlite3.connect(dst_path, timeout=60.0)
    try:
        src.backup(dst)
        dst.commit()
        row = dst.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise SystemExit(f"integrity_check failed: {row!r}")
        # Soft schema signal (fresh/legacy DBs may differ; do not invent tables)
        tables = {r[0] for r in dst.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        print("backup_ok integrity=ok tables=", ",".join(sorted(tables)) if tables else "(none)")
    finally:
        dst.close()
finally:
    src.close()
PY
}

record_db_pre_state_and_backup() {
  local wal="${DB_FILE}-wal"
  local shm="${DB_FILE}-shm"
  local state_tmp
  state_tmp="$(mktemp)"

  {
    echo "recorded_at_utc=${TS}"
    echo "db_path=${DB_FILE}"
    if [ -f "$DB_FILE" ]; then
      echo "db_existed_before_install=yes"
    else
      echo "db_existed_before_install=no"
    fi
    if [ -f "$wal" ]; then echo "wal_existed_before_install=yes"; else echo "wal_existed_before_install=no"; fi
    if [ -f "$shm" ]; then echo "shm_existed_before_install=yes"; else echo "shm_existed_before_install=no"; fi
  } >"$state_tmp"
  sudo cp -a "$state_tmp" "$DB_STATE_FILE"
  rm -f "$state_tmp"
  protect_file "$DB_STATE_FILE"

  if [ -f "$DB_FILE" ]; then
    echo "DB present: taking SQLite Backup API snapshot (WAL-safe)"
    # Destination must be writable by the python process via sudo
    sudo rm -f "${RUN_BACKUP_DIR}/asa.db.pre-migrate"
    sqlite_backup_api "$DB_FILE" "${RUN_BACKUP_DIR}/asa.db.pre-migrate"
    protect_file "${RUN_BACKUP_DIR}/asa.db.pre-migrate"
    # Convenience in-tree copy (also via Backup API)
    mkdir -p "$APP_ROOT/data/backups"
    sudo rm -f "$APP_ROOT/data/backups/asa.db.pre-migrate-${TS}"
    sqlite_backup_api "$DB_FILE" "$APP_ROOT/data/backups/asa.db.pre-migrate-${TS}"
    sudo chown asaadmin:asaadmin "$APP_ROOT/data/backups/asa.db.pre-migrate-${TS}" || true
    sudo chmod 600 "$APP_ROOT/data/backups/asa.db.pre-migrate-${TS}"
    echo "DB Backup API snapshot: OK"
  else
    echo "none" | sudo tee "${RUN_BACKUP_DIR}/asa.db.was_missing" >/dev/null
    protect_file "${RUN_BACKUP_DIR}/asa.db.was_missing"
    echo "DB absent before install: recorded explicitly (asa.db.was_missing + asa.db.state)"
  fi

  # Probe owner presence BEFORE migrate/git/service mutations (no secrets printed).
  record_owner_pre_state
}

# Probe owner presence without printing secrets.
# Always returns 0 after setting OWNER_PROBE_STATUS (callers must hard-fail on
# unreadable). Status values:
#   db_absent     — DB file does not exist yet (COUNT=0)
#   table_absent  — DB readable but admin_users missing (COUNT=0)
#   ok            — admin_users readable; COUNT is exact role=owner rows
#   unreadable    — sqlite open/query failed (COUNT empty) — NEVER treat as zero owners
probe_owner_state() {
  local db="${1:-$DB_FILE}"
  local out=""
  local rc=0
  local parse_ok=1

  OWNER_PROBE_STATUS=""
  OWNER_PROBE_COUNT=""

  if [ -z "$db" ] || [ ! -f "$db" ]; then
    OWNER_PROBE_STATUS="db_absent"
    OWNER_PROBE_COUNT=0
    return 0
  fi

  set +e
  out="$(
    DB_FILE="$db" python3 - <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

db = Path(os.environ["DB_FILE"])
try:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30.0)
except sqlite3.Error as e:
    print(f"sqlite_open_failed:{type(e).__name__}", file=sys.stderr)
    sys.exit(2)

try:
    try:
        tables = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    except sqlite3.Error as e:
        print(f"sqlite_schema_query_failed:{type(e).__name__}", file=sys.stderr)
        sys.exit(2)

    if "admin_users" not in tables:
        print("table_absent 0")
        sys.exit(0)

    try:
        n = con.execute(
            "SELECT COUNT(*) FROM admin_users WHERE role = 'owner'"
        ).fetchone()[0]
    except sqlite3.Error as e:
        print(f"sqlite_owner_count_failed:{type(e).__name__}", file=sys.stderr)
        sys.exit(2)

    print(f"ok {int(n)}")
finally:
    con.close()
PY
  )"
  rc=$?
  set -e

  if [ "$rc" -ne 0 ]; then
    OWNER_PROBE_STATUS="unreadable"
    OWNER_PROBE_COUNT=""
    echo "FATAL: DB/query unreadable while probing owners (sqlite error). Install stopped." >&2
    return 0
  fi

  # Expect: "<status> <count>" — no secrets.
  OWNER_PROBE_STATUS="${out%% *}"
  OWNER_PROBE_COUNT="${out#* }"
  case "$OWNER_PROBE_STATUS" in
    table_absent|ok)
      case "$OWNER_PROBE_COUNT" in
        ''|*[!0-9]*) parse_ok=0 ;;
      esac
      ;;
    *) parse_ok=0 ;;
  esac
  if [ "$parse_ok" -ne 1 ]; then
    OWNER_PROBE_STATUS="unreadable"
    OWNER_PROBE_COUNT=""
    echo "FATAL: owner probe returned invalid status/count. Install stopped." >&2
    return 0
  fi
  return 0
}

# Abort install when probe could not read the DB (distinct from zero owners).
require_owner_probe_readable() {
  local where="$1"
  if [ "$OWNER_PROBE_STATUS" = "unreadable" ]; then
    echo "FATAL: owner probe unreadable (${where}) — not treating as zero owners."
    fail_partial_install "failed_owner_probe_unreadable_${where// /_}"
  fi
}

record_owner_pre_state() {
  probe_owner_state
  require_owner_probe_readable "pre-change"

  case "$OWNER_PROBE_STATUS" in
    db_absent)
      OWNER_EXISTED_BEFORE="no"
      echo "owner probe (pre-change): DB absent — no owner yet"
      ;;
    table_absent)
      OWNER_EXISTED_BEFORE="no"
      echo "owner probe (pre-change): DB present but admin_users table absent"
      ;;
    ok)
      if [ "$OWNER_PROBE_COUNT" -gt 0 ]; then
        OWNER_EXISTED_BEFORE="yes"
        echo "owner probe (pre-change): owner present (count=${OWNER_PROBE_COUNT}; credentials will not be changed)"
      else
        OWNER_EXISTED_BEFORE="no"
        echo "owner probe (pre-change): admin_users present with zero role=owner"
      fi
      ;;
    *)
      echo "FATAL: unexpected owner probe status=${OWNER_PROBE_STATUS}"
      fail_partial_install "failed_owner_probe_unexpected_prestate"
      ;;
  esac

  {
    echo "owner_existed_before_install=${OWNER_EXISTED_BEFORE}"
    echo "owner_probe_status=${OWNER_PROBE_STATUS}"
    echo "owner_count_before_install=${OWNER_PROBE_COUNT}"
  } | sudo tee "${RUN_BACKUP_DIR}/owner.prestate" >/dev/null
  protect_file "${RUN_BACKUP_DIR}/owner.prestate"
}

# Collect username + password for ops.seed without echoing the password.
# Prefers existing ADMIN_USER/ADMIN_PASSWORD from the operator shell (not printed).
# Does NOT read or write .env.production.
prompt_initial_owner_credentials() {
  local user_default="${ADMIN_USER:-owner}"
  local user=""
  local p1=""
  local p2=""

  _SEED_USER=""
  _SEED_PASS=""

  if [ -n "${ADMIN_PASSWORD:-}" ]; then
    _SEED_USER="${ADMIN_USER:-owner}"
    _SEED_PASS="${ADMIN_PASSWORD}"
    echo "Using ADMIN_USER/ADMIN_PASSWORD from environment (values not printed)."
    return 0
  fi

  if [ ! -t 0 ]; then
    echo "FATAL: no role=owner and ADMIN_PASSWORD unset; cannot prompt on non-TTY"
    echo "Export ADMIN_USER/ADMIN_PASSWORD for this shell only, then re-run. .env.production untouched."
    fail_partial_install "failed_seed_no_tty"
  fi

  echo "Initial owner creation required via python -m ops.seed (before service start)."
  echo ".env.production will NOT be modified; password will not be logged."
  read -r -p "Admin username [${user_default}]: " user || true
  user="${user:-$user_default}"
  if [ "${#user}" -lt 3 ] || [ "${#user}" -gt 32 ]; then
    echo "FATAL: username must be between 3 and 32 characters"
    fail_partial_install "failed_seed_bad_username"
  fi

  while true; do
    # -s: do not echo password to the terminal
    read -r -s -p "Admin password (min 12 chars, hidden): " p1 || true
    printf '\n' >&2
    read -r -s -p "Confirm password: " p2 || true
    printf '\n' >&2
    if [ -z "$p1" ]; then
      echo "Password empty — try again." >&2
      continue
    fi
    if [ "${#p1}" -lt 12 ]; then
      echo "Password too short (min 12) — try again." >&2
      continue
    fi
    if [ "$p1" != "$p2" ]; then
      echo "Passwords do not match — try again." >&2
      continue
    fi
    break
  done
  _SEED_USER="$user"
  _SEED_PASS="$p1"
  user=""
  p1=""
  p2=""
}

# After migrate, before systemd start: ensure at least one role=owner exists.
# Existing owners are never modified. Failure aborts install (no success claim).
# DB/query errors must abort — never treated as "zero owners".
ensure_initial_owner_before_service() {
  local seed_rc=0
  local seed_out
  local seed_err

  echo "owner ensure: verifying DB + admin_users queryable after migrate"
  probe_owner_state
  require_owner_probe_readable "after migrate"

  case "$OWNER_PROBE_STATUS" in
    db_absent)
      echo "FATAL: DB file missing after migrate — cannot seed owner."
      fail_partial_install "failed_seed_db_absent_after_migrate"
      ;;
    table_absent)
      echo "FATAL: admin_users table missing after migrate — cannot seed owner."
      fail_partial_install "failed_seed_table_absent_after_migrate"
      ;;
    ok)
      ;;
    *)
      echo "FATAL: unexpected owner probe status=${OWNER_PROBE_STATUS} after migrate"
      fail_partial_install "failed_seed_unexpected_probe"
      ;;
  esac

  if [ "$OWNER_PROBE_COUNT" -gt 0 ]; then
    echo "owner ensure: already present (count=${OWNER_PROBE_COUNT}) — credentials unchanged"
    printf 'skipped_existing\n' | sudo tee "${RUN_BACKUP_DIR}/owner.seed.state" >/dev/null
    protect_file "${RUN_BACKUP_DIR}/owner.seed.state"
    return 0
  fi

  echo "owner ensure: admin_users OK with zero owners — creating initial admin is mandatory before service start"
  prompt_initial_owner_credentials

  if [ -z "${_SEED_PASS:-}" ] || [ -z "${_SEED_USER:-}" ]; then
    echo "FATAL: empty seed credentials."
    _SEED_PASS=""
    _SEED_USER=""
    fail_partial_install "failed_seed_empty_credentials"
  fi

  seed_out="$(mktemp)"
  seed_err="$(mktemp)"
  # Password only via environment for this child (project path: python -m ops.seed).
  # Never place the password on the command line / argv.
  set +e
  ADMIN_USER="${_SEED_USER}" ADMIN_PASSWORD="${_SEED_PASS}" \
    python3 -m ops.seed >"$seed_out" 2>"$seed_err"
  seed_rc=$?
  set -e

  # Wipe secrets from this shell immediately (do not leave in env or locals).
  _SEED_PASS=""
  unset ADMIN_PASSWORD 2>/dev/null || true

  if grep -Fq "generated password" "$seed_out" 2>/dev/null; then
    echo "FATAL: ops.seed emitted a generated password — ADMIN_PASSWORD was not applied"
    rm -f "$seed_out" "$seed_err"
    _SEED_USER=""
    fail_partial_install "failed_seed_generated_password_leak_path"
  fi
  # Safe status lines only (username may appear; never password).
  if [ -s "$seed_out" ]; then
    grep -E "^(created owner|user .+ already exists)" "$seed_out" || true
  fi

  if [ "$seed_rc" -ne 0 ]; then
    echo "FATAL: ops.seed failed (exit ${seed_rc})."
    rm -f "$seed_out" "$seed_err"
    _SEED_USER=""
    fail_partial_install "failed_seed_ops_seed"
  fi
  rm -f "$seed_out" "$seed_err"

  probe_owner_state
  require_owner_probe_readable "after ops.seed"
  if [ "$OWNER_PROBE_STATUS" != "ok" ] || [ "$OWNER_PROBE_COUNT" -lt 1 ]; then
    echo "FATAL: after ops.seed still no role=owner (status=${OWNER_PROBE_STATUS} count=${OWNER_PROBE_COUNT:-?})."
    echo "Existing accounts were not modified."
    _SEED_USER=""
    fail_partial_install "failed_seed_no_owner_after"
  fi

  echo "owner ensure: OK via ops.seed (password not written to logs/reports)"
  printf 'seeded_ok\n' | sudo tee "${RUN_BACKUP_DIR}/owner.seed.state" >/dev/null
  protect_file "${RUN_BACKUP_DIR}/owner.seed.state"
  _SEED_USER=""
}

require_cert_policy_before_nginx() {
  # Must run at preflight BEFORE any server mutation (backup/git/pip/migrate/systemd/nginx/certbot).
  if [ -n "${CERTBOT_EMAIL:-}" ]; then
    echo "Cert policy: CERTBOT_EMAIL is set (value not printed)"
    return 0
  fi
  if [ "${CERTBOT_SKIP_HTTPS:-}" = "1" ]; then
    echo "Cert policy: CERTBOT_SKIP_HTTPS=1 — HTTP nginx only; certbot will NOT run"
    return 0
  fi
  if [ "${CERTBOT_NO_EMAIL:-}" = "1" ]; then
    echo "Cert policy: CERTBOT_NO_EMAIL=1 — will use certbot --register-unsafely-without-email"
    return 0
  fi
  echo "FATAL: before ANY server changes you must set ONE of:"
  echo "  export CERTBOT_EMAIL='you@example.com'"
  echo "  export CERTBOT_SKIP_HTTPS=1          # explicit: no HTTPS this run"
  echo "  export CERTBOT_NO_EMAIL=1            # explicit: unsafe register without email"
  echo "No default email is assumed. Installer stopped with no changes made."
  exit 1
}

# Record whether asa-physio.service was enabled before this install mutates it.
record_systemd_enable_state() {
  local state_file="${RUN_BACKUP_DIR}/asa-physio.service.enabled.state"
  local st="missing"
  if [ -f "$UNIT_PATH" ]; then
    st="$(systemctl is-enabled asa-physio.service 2>/dev/null || true)"
    [ -n "$st" ] || st="disabled"
  else
    st="missing"
  fi
  printf '%s' "$st" | sudo tee "$state_file" >/dev/null
  protect_file "$state_file"
  echo "systemd enable state recorded: $st (contents of unit not printed)"
}

# Record active/inactive (is-active) before install mutates the service.
record_systemd_active_state() {
  local state_file="${RUN_BACKUP_DIR}/asa-physio.service.active.state"
  local st="inactive"
  if [ -f "$UNIT_PATH" ] || systemctl cat asa-physio.service >/dev/null 2>&1; then
    st="$(systemctl is-active asa-physio.service 2>/dev/null || true)"
    [ -n "$st" ] || st="inactive"
  else
    st="missing"
  fi
  printf '%s' "$st" | sudo tee "$state_file" >/dev/null
  protect_file "$state_file"
  echo "systemd active state recorded: $st"
}

# Record certbot.timer enabled + active BEFORE this install may enable/start it.
record_certbot_timer_state() {
  local en_file="${RUN_BACKUP_DIR}/certbot.timer.enabled.state"
  local act_file="${RUN_BACKUP_DIR}/certbot.timer.active.state"
  local en="missing"
  local act="missing"
  if systemctl cat certbot.timer >/dev/null 2>&1; then
    en="$(systemctl is-enabled certbot.timer 2>/dev/null || true)"
    [ -n "$en" ] || en="disabled"
    act="$(systemctl is-active certbot.timer 2>/dev/null || true)"
    [ -n "$act" ] || act="inactive"
  else
    en="missing"
    act="missing"
  fi
  printf '%s' "$en" | sudo tee "$en_file" >/dev/null
  printf '%s' "$act" | sudo tee "$act_file" >/dev/null
  protect_file "$en_file"
  protect_file "$act_file"
  echo "certbot.timer prior state recorded: enabled=$en active=$act"
}

# Restore certbot.timer to recorded prior enabled/active. Reports verification separately.
restore_certbot_timer_prior_state() {
  local en_file="${RUN_BACKUP_DIR}/certbot.timer.enabled.state"
  local act_file="${RUN_BACKUP_DIR}/certbot.timer.active.state"
  local en="missing"
  local act="missing"
  local ok=1
  CERTBOT_TIMER_RESTORE_ATTEMPTED=1
  CERTBOT_TIMER_RESTORE_VERIFIED=0

  if ! sudo test -f "$en_file"; then
    echo "certbot.timer restore: no prior enabled.state — skipped (NOT verified as restored)"
    return 0
  fi
  en="$(sudo cat "$en_file" 2>/dev/null || echo missing)"
  act="$(sudo cat "$act_file" 2>/dev/null || echo missing)"

  if ! systemctl cat certbot.timer >/dev/null 2>&1; then
    echo "certbot.timer restore: unit missing now — cannot restore (NOT verified)"
    return 1
  fi

  case "$en" in
    enabled|enabled-runtime)
      sudo systemctl enable certbot.timer || ok=0
      ;;
    disabled|masked)
      sudo systemctl disable certbot.timer 2>/dev/null || true
      ;;
    missing|not-found|static|indirect|*)
      # Was missing/unknown before: undo enable we may have done; do not claim perfect restore.
      sudo systemctl disable certbot.timer 2>/dev/null || true
      ;;
  esac

  case "$act" in
    active|activating)
      sudo systemctl start certbot.timer || ok=0
      ;;
    *)
      sudo systemctl stop certbot.timer 2>/dev/null || true
      ;;
  esac

  # Separate verification against recorded priors (best-effort match).
  local now_en now_act
  now_en="$(systemctl is-enabled certbot.timer 2>/dev/null || true)"
  now_act="$(systemctl is-active certbot.timer 2>/dev/null || true)"
  echo "certbot.timer restore check: wanted enabled=$en active=$act ; now enabled=${now_en:-?} active=${now_act:-?}"

  case "$en" in
    enabled|enabled-runtime)
      case "$now_en" in enabled|enabled-runtime) ;; *) ok=0 ;; esac
      ;;
    disabled|masked)
      case "$now_en" in disabled|masked) ;; *) ok=0 ;; esac
      ;;
  esac
  case "$act" in
    active|activating)
      case "$now_act" in active|activating) ;; *) ok=0 ;; esac
      ;;
    inactive|dead|missing)
      case "$now_act" in inactive|dead|failed|"") ;; *) ok=0 ;; esac
      ;;
  esac

  if [ "$ok" = 1 ]; then
    CERTBOT_TIMER_RESTORE_VERIFIED=1
    echo "certbot.timer restore: VERIFIED against prior state"
    return 0
  fi
  CERTBOT_TIMER_RESTORE_VERIFIED=0
  echo "certbot.timer restore: NOT verified — operator must inspect timer state"
  return 1
}

write_restore_notes() {
  # Must run only after resolve_db_path_from_env; every DB path in RESTORE uses DB_FILE.
  if [ -z "${DB_FILE:-}" ]; then
    echo "FATAL: write_restore_notes requires non-empty validated DB_FILE"
    exit 1
  fi
  if [ -z "${RESTORE_NOTES:-}" ] || [ -z "${RUN_BACKUP_DIR:-}" ]; then
    echo "FATAL: write_restore_notes missing RESTORE_NOTES/RUN_BACKUP_DIR"
    exit 1
  fi
  # On VPS: sudo tee. Local dry-run may mock sudo as identity (see local restore test).
  sudo tee "$RESTORE_NOTES" >/dev/null <<EOF
ASA Physio bootstrap restore notes — ${TS}
(No secrets are stored in this file. Do not cat env.production.bak.)

RUN_BACKUP_DIR=${RUN_BACKUP_DIR}
OVERRIDE_ENV=${OVERRIDE_ENV}
APP_ROOT=${APP_ROOT}
DB_FILE=${DB_FILE}
UPLOAD_DIR=${UPLOAD_DIR_VALIDATED}
UNIT_PATH=${UNIT_PATH}
NGINX_AVAILABLE=${NGINX_AVAILABLE}
NGINX_ENABLED=${NGINX_ENABLED}
NGINX_DEFAULT_ENABLED=${NGINX_DEFAULT_ENABLED}
DB_STATE_FILE=${DB_STATE_FILE}

Artifacts:
  ${RUN_BACKUP_DIR}/asa-physio-tree.tar.gz
      (full APP_ROOT tree excluding .venv/__pycache__ only; includes UPLOAD_DIR=${UPLOAD_DIR_VALIDATED})
  ${RUN_BACKUP_DIR}/asa-physio-tree.tar.gz.sha256
  ${RUN_BACKUP_DIR}/env.production.bak
  ${RUN_BACKUP_DIR}/env.production.bak.sha256
  ${RUN_BACKUP_DIR}/asa.db.state
  ${RUN_BACKUP_DIR}/asa.db.was_missing          (only if DB was absent before install)
  ${RUN_BACKUP_DIR}/asa.db.pre-migrate          (only if DB existed; SQLite Backup API)
  ${RUN_BACKUP_DIR}/asa.db.pre-migrate.sha256   (only if DB existed)
  ${RUN_BACKUP_DIR}/owner.prestate              (owner_existed_before / count; no secrets)
  ${RUN_BACKUP_DIR}/owner.seed.state             (skipped_existing|seeded_ok; no secrets)
  ${RUN_BACKUP_DIR}/asa-physio.service.prev     (or .prev.missing)
  ${RUN_BACKUP_DIR}/asa-physio.service.enabled.state   (enabled|disabled|missing|…)
  ${RUN_BACKUP_DIR}/asa-physio.service.active.state    (active|inactive|failed|missing|…)
  ${RUN_BACKUP_DIR}/certbot.timer.enabled.state / certbot.timer.active.state
  ${RUN_BACKUP_DIR}/nginx-asa-physio.prev       (or .prev.missing)
  ${RUN_BACKUP_DIR}/nginx-asa-physio.enabled.kind / .link / .prev
  ${RUN_BACKUP_DIR}/nginx-default.enabled.kind / .link / .prev
  ${RUN_BACKUP_DIR}/runtime-overrides.env.prev  (or .prev.missing)
  ${RUN_BACKUP_DIR}/INSTALL_INCOMPLETE / INSTALL_COMPLETE / install.phase

================================================================
FULL MANUAL ROLLBACK (run as asaadmin with sudo)
================================================================

# 0) stop app (releases SQLite locks)
sudo systemctl stop asa-physio || true

# 1) restore code tree WITHOUT mixing install leftovers into the old tree:
#    quarantine the current APP_ROOT first (no silent delete), then extract backup.
TREE_Q=${RUN_BACKUP_DIR}/quarantine-tree-before-restore
sudo mkdir -p "\$TREE_Q"
sudo chmod 700 "\$TREE_Q"
if [ -e ${APP_ROOT} ]; then
  sudo mv ${APP_ROOT} "\$TREE_Q/asa-physio.pre-restore-\$(date -u +%Y%m%d-%H%M%S)"
  echo "Current project tree moved to quarantine (not silently deleted)."
fi
# Verify tree backup checksum before extract (optional but recommended)
if [ -f ${RUN_BACKUP_DIR}/asa-physio-tree.tar.gz.sha256 ]; then
  sudo sha256sum -c ${RUN_BACKUP_DIR}/asa-physio-tree.tar.gz.sha256
fi
sudo tar -xzf ${RUN_BACKUP_DIR}/asa-physio-tree.tar.gz -C /opt
echo "Pre-install tree extracted from asa-physio-tree.tar.gz (includes UPLOAD_DIR=${UPLOAD_DIR_VALIDATED} when under APP_ROOT)"

# 1b) rebuild .venv (excluded from tree tar) from restored requirements.txt
cd ${APP_ROOT}
echo "Python for venv rebuild: \$(python3 --version 2>&1)"
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "WARNING: python3 is older than 3.10 — confirm project compatibility before pip install."
fi
rm -rf ${APP_ROOT}/.venv
python3 -m venv ${APP_ROOT}/.venv
# shellcheck disable=SC1091
source ${APP_ROOT}/.venv/bin/activate
pip install -U pip
pip install -r ${APP_ROOT}/requirements.txt
python -c "import fastapi, uvicorn; print('venv_ok', fastapi.__version__)"
deactivate || true
echo "venv rebuilt from restored requirements.txt (operator must confirm app starts)."

# 2) DATABASE rollback — read state file first
#    cat ${DB_STATE_FILE}
#
# 2a) If db_existed_before_install=no (or asa.db.was_missing exists):
#     Install may have CREATED a new DB incompatible with old code.
#     Quarantine current DB artifacts (do NOT silent-delete), then leave DB absent.
if [ -f ${RUN_BACKUP_DIR}/asa.db.was_missing ] || grep -q 'db_existed_before_install=no' ${DB_STATE_FILE} 2>/dev/null; then
  QDIR=${RUN_BACKUP_DIR}/quarantine-db-after-install
  sudo mkdir -p "\$QDIR"
  sudo chmod 700 "\$QDIR"
  for f in ${DB_FILE} ${DB_FILE}-wal ${DB_FILE}-shm; do
    if [ -e "\$f" ]; then
      sudo mv "\$f" "\$QDIR/\$(basename "\$f").quarantined-\$(date -u +%Y%m%d-%H%M%S)"
    fi
  done
  echo "DB was absent before install: current db/wal/shm moved to quarantine (not silently deleted)."
fi
#
# 2b) If db_existed_before_install=yes: restore Backup API snapshot, then drop WAL/SHM
#     so SQLite does not replay a mismatched journal against the restored main file.
if [ -f ${RUN_BACKUP_DIR}/asa.db.pre-migrate ]; then
  if [ -f ${RUN_BACKUP_DIR}/asa.db.pre-migrate.sha256 ]; then
    sudo sha256sum -c ${RUN_BACKUP_DIR}/asa.db.pre-migrate.sha256
  fi
  QDIR=${RUN_BACKUP_DIR}/quarantine-db-before-restore
  sudo mkdir -p "\$QDIR"
  sudo chmod 700 "\$QDIR"
  for f in ${DB_FILE} ${DB_FILE}-wal ${DB_FILE}-shm; do
    if [ -e "\$f" ]; then
      sudo mv "\$f" "\$QDIR/\$(basename "\$f").pre-restore-\$(date -u +%Y%m%d-%H%M%S)"
    fi
  done
  sudo cp -a ${RUN_BACKUP_DIR}/asa.db.pre-migrate ${DB_FILE}
  sudo chown asaadmin:asaadmin ${DB_FILE}
  sudo chmod 600 ${DB_FILE}
  # Ensure no stale wal/shm remain next to restored main
  sudo rm -f ${DB_FILE}-wal ${DB_FILE}-shm
  echo "DB restored from Backup API snapshot; wal/shm cleared after quarantine of prior files."
  echo "NOTE: DB restore is MANUAL — installer never auto-claims this succeeded."
fi

# 3) restore .env.production if needed (do not cat the bak file)
if [ -f ${RUN_BACKUP_DIR}/env.production.bak ]; then
  sudo cp -a ${RUN_BACKUP_DIR}/env.production.bak ${APP_ROOT}/.env.production
  sudo chown asaadmin:asaadmin ${APP_ROOT}/.env.production
  sudo chmod 600 ${APP_ROOT}/.env.production
fi

# 4) restore systemd unit (do NOT start yet — overrides must be restored first)
if [ -f ${RUN_BACKUP_DIR}/asa-physio.service.prev ]; then
  sudo cp -a ${RUN_BACKUP_DIR}/asa-physio.service.prev ${UNIT_PATH}
elif [ -f ${RUN_BACKUP_DIR}/asa-physio.service.prev.missing ]; then
  sudo rm -f ${UNIT_PATH}
fi

# 5) restore runtime-overrides.env BEFORE any service start (EnvironmentFile order)
# MARKER: overrides-before-service-start
if [ -f ${RUN_BACKUP_DIR}/runtime-overrides.env.prev ]; then
  sudo mkdir -p ${OVERRIDE_DIR}
  sudo cp -a ${RUN_BACKUP_DIR}/runtime-overrides.env.prev ${OVERRIDE_ENV}
  sudo chown root:root ${OVERRIDE_ENV}
  sudo chmod 600 ${OVERRIDE_ENV}
elif [ -f ${RUN_BACKUP_DIR}/runtime-overrides.env.prev.missing ]; then
  sudo rm -f ${OVERRIDE_ENV}
fi

# 5b) reload unit definitions after unit + overrides are on disk
sudo systemctl daemon-reload

# 5c) restore exact prior enable state (does not start the service)
_en=\$(sudo cat ${RUN_BACKUP_DIR}/asa-physio.service.enabled.state 2>/dev/null || echo missing)
case "\$_en" in
  enabled|enabled-runtime)
    if [ -f ${UNIT_PATH} ]; then
      sudo systemctl enable asa-physio.service
    fi
    ;;
  disabled)
    sudo systemctl disable asa-physio.service 2>/dev/null || true
    ;;
  missing|not-found|*)
    # Unit did not exist (or unknown): do not leave an enable symlink behind.
    sudo systemctl disable asa-physio.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/multi-user.target.wants/asa-physio.service
    ;;
esac

# 5d) apply prior active/inactive ONLY after overrides restored —
#     NEVER start only because the unit file exists.
# MARKER: service-start-after-overrides
_act=\$(sudo cat ${RUN_BACKUP_DIR}/asa-physio.service.active.state 2>/dev/null || echo inactive)
case "\$_act" in
  active|activating|reloading)
    if [ -f ${UNIT_PATH} ]; then
      sudo systemctl start asa-physio.service
    else
      echo "Prior active=\$_act but unit missing — not starting."
    fi
    ;;
  *)
    sudo systemctl stop asa-physio.service 2>/dev/null || true
    echo "Service left stopped (prior active=\$_act)."
    ;;
esac

# 5e) restore prior certbot.timer enabled/active (verify manually afterward)
if [ -f ${RUN_BACKUP_DIR}/certbot.timer.enabled.state ]; then
  _ct_en=\$(sudo cat ${RUN_BACKUP_DIR}/certbot.timer.enabled.state)
  _ct_act=\$(sudo cat ${RUN_BACKUP_DIR}/certbot.timer.active.state 2>/dev/null || echo inactive)
  if systemctl cat certbot.timer >/dev/null 2>&1; then
    case "\$_ct_en" in
      enabled|enabled-runtime) sudo systemctl enable certbot.timer ;;
      disabled|masked) sudo systemctl disable certbot.timer 2>/dev/null || true ;;
      *) sudo systemctl disable certbot.timer 2>/dev/null || true ;;
    esac
    case "\$_ct_act" in
      active|activating) sudo systemctl start certbot.timer ;;
      *) sudo systemctl stop certbot.timer 2>/dev/null || true ;;
    esac
    echo "certbot.timer restore attempted: wanted enabled=\$_ct_en active=\$_ct_act"
    echo "certbot.timer now: enabled=\$(systemctl is-enabled certbot.timer 2>/dev/null || true) active=\$(systemctl is-active certbot.timer 2>/dev/null || true)"
    echo "Operator must confirm timer match — SUCCESS NOT CLAIMED automatically."
  else
    echo "certbot.timer unit missing — cannot restore prior timer state."
  fi
fi

# 6) restore nginx sites-available
if [ -f ${RUN_BACKUP_DIR}/nginx-asa-physio.prev ]; then
  sudo cp -a ${RUN_BACKUP_DIR}/nginx-asa-physio.prev ${NGINX_AVAILABLE}
elif [ -f ${RUN_BACKUP_DIR}/nginx-asa-physio.prev.missing ]; then
  sudo rm -f ${NGINX_AVAILABLE}
fi

# 7) restore sites-enabled/asa-physio exactly
_kind=\$(sudo cat ${RUN_BACKUP_DIR}/nginx-asa-physio.enabled.kind 2>/dev/null || echo missing)
case "\$_kind" in
  symlink)
    _link=\$(sudo cat ${RUN_BACKUP_DIR}/nginx-asa-physio.enabled.link)
    sudo ln -sfn "\$_link" ${NGINX_ENABLED}
    ;;
  file)
    sudo cp -a ${RUN_BACKUP_DIR}/nginx-asa-physio.enabled.prev ${NGINX_ENABLED}
    ;;
  missing|*)
    sudo rm -f ${NGINX_ENABLED}
    ;;
esac

# 8) restore sites-enabled/default exactly
_kind=\$(sudo cat ${RUN_BACKUP_DIR}/nginx-default.enabled.kind 2>/dev/null || echo missing)
case "\$_kind" in
  symlink)
    _link=\$(sudo cat ${RUN_BACKUP_DIR}/nginx-default.enabled.link)
    sudo ln -sfn "\$_link" ${NGINX_DEFAULT_ENABLED}
    ;;
  file)
    sudo cp -a ${RUN_BACKUP_DIR}/nginx-default.enabled.prev ${NGINX_DEFAULT_ENABLED}
    ;;
  missing|*)
    sudo rm -f ${NGINX_DEFAULT_ENABLED}
    ;;
esac

sudo nginx -t && sudo systemctl reload nginx

# 9) active/inactive applied in 5d after overrides — do not start merely because unit exists

# 10) Verify manually — do NOT claim rollback success until health checks pass:
#   curl -fsS http://127.0.0.1:8080/api/healthz
#   curl -fsS http://127.0.0.1:8080/api/readyz
#   sudo nginx -t
echo "RESTORE procedure finished issuing commands — SUCCESS NOT CLAIMED until operator verifies."
EOF
  # Sanity before chmod 600: RESTORE must embed validated DB_FILE (and wal/shm siblings).
  # Bootstrap dir is root:root 700 and RESTORE is written via sudo tee — operator cannot
  # read it without root. All checks MUST use sudo grep (do not chmod the dir world-readable).
  if ! sudo grep -Fq -- "DB_FILE=${DB_FILE}" "$RESTORE_NOTES"; then
    echo "FATAL: RESTORE.txt missing validated DB_FILE=${DB_FILE}"
    exit 1
  fi
  if ! sudo grep -Fq -- "${DB_FILE}-wal" "$RESTORE_NOTES" \
    || ! sudo grep -Fq -- "${DB_FILE}-shm" "$RESTORE_NOTES"; then
    echo "FATAL: RESTORE.txt missing ${DB_FILE}-wal/${DB_FILE}-shm rollback paths"
    exit 1
  fi
  if ! sudo grep -Fq -- "asa.db.pre-migrate ${DB_FILE}" "$RESTORE_NOTES"; then
    echo "FATAL: RESTORE.txt restore-copy target is not validated DB_FILE"
    exit 1
  fi
  if ! sudo grep -Fq -- "quarantine-tree-before-restore" "$RESTORE_NOTES"; then
    echo "FATAL: RESTORE.txt missing tree quarantine step"
    exit 1
  fi
  if [ -z "${UPLOAD_DIR_VALIDATED:-}" ]; then
    echo "FATAL: write_restore_notes requires non-empty UPLOAD_DIR_VALIDATED"
    exit 1
  fi
  if ! sudo grep -Fq -- "UPLOAD_DIR=${UPLOAD_DIR_VALIDATED}" "$RESTORE_NOTES"; then
    echo "FATAL: RESTORE.txt missing validated UPLOAD_DIR=${UPLOAD_DIR_VALIDATED}"
    exit 1
  fi
  if ! sudo grep -Fq -- "includes UPLOAD_DIR=${UPLOAD_DIR_VALIDATED}" "$RESTORE_NOTES"; then
    echo "FATAL: RESTORE.txt missing tree-tar UPLOAD_DIR coverage note"
    exit 1
  fi
  if ! sudo grep -Fq -- "asa-physio.service.enabled.state" "$RESTORE_NOTES"; then
    echo "FATAL: RESTORE.txt missing systemd enable-state restore"
    exit 1
  fi
  if ! sudo grep -Fq -- "asa-physio.service.active.state" "$RESTORE_NOTES"; then
    echo "FATAL: RESTORE.txt missing systemd active-state restore"
    exit 1
  fi
  if ! sudo grep -Fq -- "certbot.timer.enabled.state" "$RESTORE_NOTES"; then
    echo "FATAL: RESTORE.txt missing certbot.timer state restore"
    exit 1
  fi
  if ! sudo grep -Fq -- "NEVER start only because the unit file exists" "$RESTORE_NOTES" \
    && ! sudo grep -Fq -- "do not start merely because unit exists" "$RESTORE_NOTES"; then
    echo "FATAL: RESTORE.txt missing active-state start guard wording"
    exit 1
  fi
  # Order: overrides restore marker must appear before service-start marker.
  _ov_line="$(sudo grep -n -- 'MARKER: overrides-before-service-start' "$RESTORE_NOTES" | head -n1 | cut -d: -f1 || true)"
  _st_line="$(sudo grep -n -- 'MARKER: service-start-after-overrides' "$RESTORE_NOTES" | head -n1 | cut -d: -f1 || true)"
  if [ -z "${_ov_line:-}" ] || [ -z "${_st_line:-}" ] || [ "$_ov_line" -ge "$_st_line" ]; then
    echo "FATAL: RESTORE.txt must restore runtime-overrides.env before starting the service"
    exit 1
  fi
  if ! sudo grep -Fq -- "rebuild .venv" "$RESTORE_NOTES" \
    && ! sudo grep -Fq -- "venv rebuilt" "$RESTORE_NOTES"; then
    if ! sudo grep -Fq -- "python3 -m venv" "$RESTORE_NOTES"; then
      echo "FATAL: RESTORE.txt missing venv rebuild steps"
      exit 1
    fi
  fi
  protect_file "$RESTORE_NOTES"
}

ensure_certbot_timer_and_dry_run() {
  CERTBOT_TIMER_OK=0
  CERTBOT_RENEW_DRY_RUN_OK=0
  echo "=== certbot.timer (auto-renew) + renew dry-run ==="
  if ! systemctl list-unit-files --type=timer 2>/dev/null | grep -q '^certbot\.timer'; then
    if ! systemctl cat certbot.timer >/dev/null 2>&1; then
      echo "FATAL: certbot.timer unit not found — cannot confirm automatic TLS renewal"
      return 1
    fi
  fi
  sudo systemctl enable certbot.timer
  sudo systemctl start certbot.timer
  if systemctl is-enabled certbot.timer >/dev/null 2>&1; then
    echo "certbot.timer: enabled"
  else
    echo "FATAL: certbot.timer failed to enable"
    return 1
  fi
  # timer "active" means waiting/running schedule
  if systemctl is-active certbot.timer >/dev/null 2>&1; then
    echo "certbot.timer: active"
    CERTBOT_TIMER_OK=1
  else
    echo "FATAL: certbot.timer is not active after enable/start"
    return 1
  fi
  if sudo certbot renew --dry-run; then
    CERTBOT_RENEW_DRY_RUN_OK=1
    echo "certbot renew --dry-run: OK"
  else
    echo "FATAL: certbot renew --dry-run failed"
    return 1
  fi
  return 0
}

# Deadline clock in nanoseconds. Overridable in tests via redefining this function.
#
# Preferred (Ubuntu/production): Python time.monotonic_ns() — monotonic, ns precision,
# immune to NTP/settime jumps.
#
# Fallback 1: GNU `date +%s%N` — WALL clock with ns; NOT monotonic (can jump).
# Fallback 2: `date +%s` expanded to ns — WALL clock, PRECISION LIMITED TO 1 SECOND;
#   near the deadline this may over-budget a curl by up to ~1s versus true elapsed time.
_asa_healthz_now_ns() {
  local raw py
  for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1; then
      raw="$("$py" -c 'import time; print(time.monotonic_ns())' 2>/dev/null || true)"
      if [[ "$raw" =~ ^[0-9]+$ ]]; then
        printf '%s\n' "$raw"
        return 0
      fi
    fi
  done
  raw="$(date +%s%N 2>/dev/null || true)"
  if [[ "$raw" =~ ^[0-9]+$ ]] && [ "${#raw}" -ge 13 ]; then
    printf '%s\n' "$raw"
    return 0
  fi
  printf '%s000000000\n' "$(date +%s)"
}

# Sleep helper (seconds, may be fractional). Overridable in tests.
_asa_healthz_sleep() { sleep "$1"; }

# Format nanoseconds budget as curl --max-time / --connect-timeout (seconds, 3dp).
# Truncates DOWN to whole milliseconds so the printed value never exceeds remaining_ns.
# Emits 0.001 only when ns >= 1_000_000; smaller budgets become 0.000 (caller should not curl).
_asa_healthz_ns_to_curl_s() {
  awk -v ns="$1" 'BEGIN {
    if (ns < 0) ns = 0
    ms = int(ns / 1000000)
    printf "%d.%03d", int(ms / 1000), ms % 1000
  }'
}

# Poll local uvicorn health until ready (startup can exceed a fixed sleep).
# Deadline is high-resolution wall clock (default 60s), not iteration count.
# Each curl budget is derived from remaining time (capped ~1s); no second body fetch.
# Usage: wait_for_local_healthz [url] [max_seconds]
# Returns 0 when curl succeeds before deadline; 1 on timeout. Does not roll back.
wait_for_local_healthz() {
  local url="${1:-http://127.0.0.1:8080/api/healthz}"
  local max_sec="${2:-60}"
  local start_ns now_ns deadline_ns remaining_ns budget_ns poll_ns body elapsed_ns
  local mt sl curl_body_tmp
  local curl_cap_ns=1000000000
  local min_curl_ns=1000000

  start_ns="$(_asa_healthz_now_ns)"
  deadline_ns=$((start_ns + max_sec * 1000000000))
  echo "Waiting for ${url} (poll 1s, up to ${max_sec}s wall clock)..."

  while true; do
    now_ns="$(_asa_healthz_now_ns)"
    remaining_ns=$((deadline_ns - now_ns))
    if [ "$remaining_ns" -le 0 ]; then
      break
    fi
    if [ "$remaining_ns" -lt "$min_curl_ns" ]; then
      break
    fi

    budget_ns="$curl_cap_ns"
    if [ "$budget_ns" -gt "$remaining_ns" ]; then
      budget_ns="$remaining_ns"
    fi
    mt="$(_asa_healthz_ns_to_curl_s "$budget_ns")"

    body=""
    curl_body_tmp="$(mktemp)"
    if curl -fsS --connect-timeout "$mt" --max-time "$mt" -o "$curl_body_tmp" "$url" 2>/dev/null; then
      now_ns="$(_asa_healthz_now_ns)"
      if [ "$now_ns" -gt "$deadline_ns" ]; then
        rm -f "$curl_body_tmp"
        echo "FATAL: ${url} responded after ${max_sec}s deadline"
        return 1
      fi
      body="$(<"$curl_body_tmp")"
      rm -f "$curl_body_tmp"
      elapsed_ns=$((now_ns - start_ns))
      echo "healthz OK after $(awk -v ns="$elapsed_ns" 'BEGIN{printf "%.3f", ns/1e9}')s"
      # Body from the same request (no second curl that could overrun the deadline).
      printf '%s\n' "$body"
      return 0
    fi
    rm -f "$curl_body_tmp"

    now_ns="$(_asa_healthz_now_ns)"
    remaining_ns=$((deadline_ns - now_ns))
    if [ "$remaining_ns" -le 0 ]; then
      break
    fi

    poll_ns=1000000000
    if [ "$poll_ns" -gt "$remaining_ns" ]; then
      poll_ns="$remaining_ns"
    fi
    sl="$(awk -v ns="$poll_ns" 'BEGIN{printf "%.3f", ns/1e9}')"
    _asa_healthz_sleep "$sl"
  done

  echo "FATAL: ${url} not ready within ${max_sec}s"
  return 1
}

# Emit systemd/journal diagnostics before rollback (no secrets / no env dump).
report_service_smoke_diagnostics() {
  echo "=== diagnostics before rollback (asa-physio.service) ==="
  systemctl status asa-physio.service --no-pager -l 2>&1 || true
  systemctl show asa-physio.service \
    -p MainPID -p ActiveState -p SubState -p Result -p ExecMainStatus -p NRestarts 2>&1 || true
  echo "MainPID=$(systemctl show -p MainPID --value asa-physio.service 2>/dev/null || echo unknown)"
  # Recent logs only; do not cat EnvironmentFile / .env.
  if command -v journalctl >/dev/null 2>&1; then
    sudo journalctl -u asa-physio.service -n 80 --no-pager 2>&1 || true
  fi
}

fail_partial_install() {
  local reason="$1"
  # exit does not re-enter ERR, but clear trap anyway to avoid any nested set -e surprises.
  if [ "$HANDLING_FAILURE" = 1 ]; then
    echo "FATAL: $reason (failure handler already active — skipping duplicate rollback/hints)"
    exit 1
  fi
  HANDLING_FAILURE=1
  trap - ERR
  INSTALL_OUTCOME="$reason"
  echo "FATAL: $reason — install is PARTIAL/FAILED; success NOT claimed."
  mark_install_incomplete || true
  case "$INSTALL_PHASE" in
    service|nginx|certbot|seeded|migrated)
      # Never auto-restore DB/tree; optional non-DB restore only (includes service active + certbot.timer).
      safe_non_db_rollback_attempt || true
      ;;
  esac
  print_manual_restore_and_continue_hints
  if [ "$NON_DB_ROLLBACK_ATTEMPTED" = 1 ] && [ "$NON_DB_ROLLBACK_VERIFIED" != 1 ]; then
    echo "Non-DB rollback was attempted but NOT verified — do not assume services are restored."
  fi
  if [ "$CERTBOT_TIMER_RESTORE_ATTEMPTED" = 1 ] && [ "$CERTBOT_TIMER_RESTORE_VERIFIED" != 1 ]; then
    echo "certbot.timer restore was attempted but NOT verified — inspect timer separately."
  fi
  exit 1
}

on_err() {
  local ec=$?
  if [ "$HANDLING_FAILURE" = 1 ]; then
    exit "$ec"
  fi
  HANDLING_FAILURE=1
  trap - ERR
  INSTALL_OUTCOME="failed_other"
  echo "ERROR: installer failed (exit ${ec}) at phase=${INSTALL_PHASE}. No success claimed."
  echo "Backups (if any) are under: ${RUN_BACKUP_DIR}"
  mark_install_incomplete || true
  # Auto DB/tree rollback is intentionally NOT performed (migration compatibility).
  case "$INSTALL_PHASE" in
    service|nginx|certbot|seeded|migrated)
      safe_non_db_rollback_attempt || true
      ;;
  esac
  print_manual_restore_and_continue_hints
  if [ "$NON_DB_ROLLBACK_ATTEMPTED" = 1 ] && [ "$NON_DB_ROLLBACK_VERIFIED" != 1 ]; then
    echo "Non-DB rollback was attempted but NOT verified — do not assume services are restored."
  fi
  if [ "$CERTBOT_TIMER_RESTORE_ATTEMPTED" = 1 ] && [ "$CERTBOT_TIMER_RESTORE_VERIFIED" != 1 ]; then
    echo "certbot.timer restore was attempted but NOT verified — inspect timer separately."
  fi
  exit "$ec"
}
trap on_err ERR

echo "=== preflight ==="
set_install_phase "preflight"
[ -d "$APP_ROOT" ] || { echo "missing $APP_ROOT"; exit 1; }
cd "$APP_ROOT"
[ -f "$APP_ROOT/.env.production" ] || { echo "FATAL: .env.production missing"; exit 1; }
echo "env file present: yes (contents not shown)"
stat -c "env meta: owner=%U:%G mode=%a size=%s" "$APP_ROOT/.env.production" || true

echo "=== cert policy gate (before ANY server mutation) ==="
require_cert_policy_before_nginx

echo "=== resolve DB_PATH from .env.production (before backup/migrate) ==="
resolve_db_path_from_env || exit 1

echo "=== resolve UPLOAD_DIR from .env.production (must stay under APP_ROOT) ==="
resolve_upload_dir_from_env || exit 1

echo "=== verify prior backup (checksum only, via root; no backup contents printed) ==="
if sudo test -f "$BACKUP_TAR"; then
  # sha256sum -c reads the archive for hashing only; it does not dump file contents.
  printf '%s  %s\n' "$BACKUP_SHA256" "$BACKUP_TAR" | sudo sha256sum -c -
else
  echo "WARN: prior backup tar not found: $BACKUP_TAR"
fi

echo "=== create run backup directory (never overwrites other bootstrap-* runs) ==="
protect_dir "$BACKUP_ROOT"
protect_dir "$RUN_BACKUP_DIR"
warn_previous_incomplete_runs
mark_install_incomplete

echo "=== new safety backups (before any mutate) ==="
echo "Tree tar will include UPLOAD_DIR=${UPLOAD_DIR_VALIDATED} (validated under APP_ROOT; .venv excluded)"
sudo tar -czf "${RUN_BACKUP_DIR}/asa-physio-tree.tar.gz" \
  --exclude='.venv' \
  --exclude='__pycache__' \
  -C /opt asa-physio
protect_file "${RUN_BACKUP_DIR}/asa-physio-tree.tar.gz"
sha256_record_and_verify "${RUN_BACKUP_DIR}/asa-physio-tree.tar.gz"

sudo cp -a "$APP_ROOT/.env.production" "${RUN_BACKUP_DIR}/env.production.bak"
protect_file "${RUN_BACKUP_DIR}/env.production.bak"
sha256_record_and_verify "${RUN_BACKUP_DIR}/env.production.bak"

mkdir -p "$APP_ROOT/data/uploads" "$APP_ROOT/data/backups"
record_db_pre_state_and_backup
if sudo test -f "${RUN_BACKUP_DIR}/asa.db.pre-migrate"; then
  sha256_record_and_verify "${RUN_BACKUP_DIR}/asa.db.pre-migrate"
fi

if [ -f "$UNIT_PATH" ]; then
  sudo cp -a "$UNIT_PATH" "${RUN_BACKUP_DIR}/asa-physio.service.prev"
  protect_file "${RUN_BACKUP_DIR}/asa-physio.service.prev"
  echo "systemd unit backup: yes"
else
  echo "none" | sudo tee "${RUN_BACKUP_DIR}/asa-physio.service.prev.missing" >/dev/null
  protect_file "${RUN_BACKUP_DIR}/asa-physio.service.prev.missing"
  echo "systemd unit backup: none existed"
fi
record_systemd_enable_state
record_systemd_active_state
record_certbot_timer_state

if [ -f "$NGINX_AVAILABLE" ]; then
  sudo cp -a "$NGINX_AVAILABLE" "${RUN_BACKUP_DIR}/nginx-asa-physio.prev"
  protect_file "${RUN_BACKUP_DIR}/nginx-asa-physio.prev"
  echo "nginx sites-available backup: yes"
else
  echo "none" | sudo tee "${RUN_BACKUP_DIR}/nginx-asa-physio.prev.missing" >/dev/null
  protect_file "${RUN_BACKUP_DIR}/nginx-asa-physio.prev.missing"
  echo "nginx sites-available backup: none existed"
fi

backup_enabled_path "$NGINX_ENABLED" "nginx-asa-physio.enabled"
backup_enabled_path "$NGINX_DEFAULT_ENABLED" "nginx-default.enabled"

if [ -f "$OVERRIDE_ENV" ]; then
  sudo cp -a "$OVERRIDE_ENV" "${RUN_BACKUP_DIR}/runtime-overrides.env.prev"
  protect_file "${RUN_BACKUP_DIR}/runtime-overrides.env.prev"
  echo "runtime-overrides.env backup: yes (previous file existed)"
else
  echo "none" | sudo tee "${RUN_BACKUP_DIR}/runtime-overrides.env.prev.missing" >/dev/null
  protect_file "${RUN_BACKUP_DIR}/runtime-overrides.env.prev.missing"
  echo "runtime-overrides.env backup: none existed"
fi

write_restore_notes
echo "RESTORE.txt written (executable rollback commands; no secrets)"

echo "=== re-verify this-run backup SHA-256 before ANY mutate ==="
sha256_verify_existing "${RUN_BACKUP_DIR}/asa-physio-tree.tar.gz"
sha256_verify_existing "${RUN_BACKUP_DIR}/env.production.bak"
if sudo test -f "${RUN_BACKUP_DIR}/asa.db.pre-migrate"; then
  sha256_verify_existing "${RUN_BACKUP_DIR}/asa.db.pre-migrate"
fi
set_install_phase "backups_verified"
echo "Backups verified — mutations may proceed."

echo "=== git status before pin (no push) ==="
git remote -v || true
git status --short --branch || true
git rev-parse HEAD 2>/dev/null || echo "HEAD unknown"
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "FATAL: local tracked modifications present. Commit/stash/copy them aside before pin."
  echo "Uncommitted changes were NOT discarded. Installer stopped before checkout."
  exit 1
fi
untracked="$(git ls-files --others --exclude-standard | head -n 50 || true)"
if [ -n "$untracked" ]; then
  echo "NOTE: untracked files present (will not be deleted by checkout). Sample names only:"
  echo "$untracked" | head -n 20
fi

echo "=== git pin to exact SHA (no force-push) ==="
set_install_phase "mutating"
git fetch origin
git rev-parse --verify "${TARGET_SHA}^{commit}" >/dev/null
git checkout -B main "$TARGET_SHA"
test "$(git rev-parse HEAD)" = "$TARGET_SHA"
echo "HEAD pinned to ${TARGET_SHA}"

echo "=== venv ==="
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "WARNING: python3 < 3.10 — confirm runtime compatibility with this release."
fi
echo "python3: $(python3 --version 2>&1)"
[ -x "$APP_ROOT/.venv/bin/python" ] || python3 -m venv "$APP_ROOT/.venv"
# shellcheck disable=SC1091
source "$APP_ROOT/.venv/bin/activate"
pip install -q -r requirements.txt
python -c "import fastapi, uvicorn; print('deps_ok')"

echo "=== migrate (forward-only; preserve DB + uploads) ==="
set -a
# shellcheck disable=SC1091
source "$APP_ROOT/.env.production"
set +a
# Force validated paths so migrate/backup/rollback stay on the same DB/uploads tree.
export DB_PATH="$DB_FILE"
export UPLOAD_DIR="$UPLOAD_DIR_VALIDATED"
# Bind safe sinks IMMEDIATELY after sourcing .env so migrate/seed/shell never use
# production SMS.ir credentials even before runtime-overrides.env exists.
# Values are non-secret sink URLs; do not print other env contents.
export REMINDER_BACKGROUND=0
export SMS_PROVIDER=webhook
export SMS_WEBHOOK=http://127.0.0.1:9/asa-sms-sink
export NOTIFY_PROVIDER=webhook
export NOTIFY_WEBHOOK=http://127.0.0.1:9/asa-notify-sink
echo "install-shell SMS/notify/reminder sinks bound (webhook + REMINDER_BACKGROUND=0; no SMS.ir)."
# Re-check after source: reject any attempt to escape APP_ROOT via sourced env.
case "$UPLOAD_DIR" in
  "$APP_ROOT"/*) ;;
  *)
    echo "FATAL: effective UPLOAD_DIR escaped APP_ROOT after sourcing .env.production"
    fail_partial_install "failed_upload_dir_outside_app_root"
    ;;
esac
mkdir -p "$UPLOAD_DIR"
mkdir -p "$(dirname "$DB_FILE")"
python3 -m api.db
python3 - <<'PY'
import os, sqlite3
from pathlib import Path
db = Path(os.environ.get("DB_PATH", "data/asa.db"))
con = sqlite3.connect(db)
applied = [r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY version")]
tables = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
)]
print("migrations:", ",".join(applied))
print("tables:", ",".join(tables))
con.close()
PY
set_install_phase "migrated"
echo "NOTE: after migrate, DB auto-rollback is disabled (compatibility risk). Use RESTORE.txt manually if needed."

echo "=== initial owner (ops.seed; before systemd start) ==="
# Mandatory if no role=owner after migrate. Does not modify .env.production.
# Existing owners are left unchanged. Failure aborts before service start.
# Password via ADMIN_PASSWORD env only (ops.seed limitation — not absolute secrecy).
ensure_initial_owner_before_service
set_install_phase "seeded"

echo "=== SMS sink override file (loaded AFTER .env.production) ==="
sudo mkdir -p "$OVERRIDE_DIR"
sudo chown root:root "$OVERRIDE_DIR"
sudo chmod 755 "$OVERRIDE_DIR"
sudo tee "$OVERRIDE_ENV" >/dev/null <<'OVR'
# ASA Physio — temporary runtime overrides (no real SMS.ir).
# Loaded after /opt/asa-physio/.env.production so these keys win.
REMINDER_BACKGROUND=0
SMS_PROVIDER=webhook
SMS_WEBHOOK=http://127.0.0.1:9/asa-sms-sink
NOTIFY_PROVIDER=webhook
NOTIFY_WEBHOOK=http://127.0.0.1:9/asa-notify-sink
OVR
sudo chown root:root "$OVERRIDE_ENV"
sudo chmod 600 "$OVERRIDE_ENV"
echo "override keys present:"
sudo grep -E '^(REMINDER_BACKGROUND|SMS_PROVIDER|SMS_WEBHOOK|NOTIFY_PROVIDER|NOTIFY_WEBHOOK)=' \
  "$OVERRIDE_ENV"

echo "=== write systemd unit ==="
# ReadWritePaths must include validated UPLOAD_DIR (always under APP_ROOT).
# UNIT heredoc is unquoted only for UPLOAD_DIR_VALIDATED expansion — no secrets.
sudo tee "$UNIT_PATH" >/dev/null <<UNIT
[Unit]
Description=ASA Physio FastAPI (uvicorn)
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=asaadmin
Group=asaadmin
WorkingDirectory=/opt/asa-physio
# Order matters: later EnvironmentFile overrides earlier for the same variable.
EnvironmentFile=-/opt/asa-physio/.env.production
EnvironmentFile=-/etc/asa-physio/runtime-overrides.env
Environment=WORKERS=1
ExecStart=/opt/asa-physio/.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8080 --workers 1 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=on-failure
RestartSec=5
ReadWritePaths=/opt/asa-physio/data ${UPLOAD_DIR_VALIDATED}
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable asa-physio.service
sudo systemctl restart asa-physio.service
set_install_phase "service"
# Uvicorn often needs >2s after systemd start; poll healthz instead of a fixed sleep.
if ! wait_for_local_healthz "http://127.0.0.1:8080/api/healthz" 60; then
  report_service_smoke_diagnostics
  fail_partial_install "failed_smoke_healthz"
fi
READY_JSON="$(curl -fsS "http://127.0.0.1:8080/api/readyz")" || fail_partial_install "failed_smoke_readyz"
echo "$READY_JSON"
IDX_CODE="$(curl -sS -o /dev/null -w "%{http_code}" --connect-timeout 5 "http://127.0.0.1:8080/" || true)"
case "$IDX_CODE" in
  200|301|302|303|307|308) echo "index ${IDX_CODE}" ;;
  *) fail_partial_install "failed_smoke_index_http_${IDX_CODE:-000}" ;;
esac

echo "=== verify effective SMS/notify/reminder env in running process ==="
PID="$(systemctl show -p MainPID --value asa-physio)"
if [ -z "$PID" ] || [ "$PID" = "0" ]; then
  echo "FATAL: asa-physio has no MainPID — cannot verify SMS sink"
  fail_partial_install "failed_sms_verify_no_pid"
fi
EFF="$(sudo cat "/proc/${PID}/environ" | tr '\0' '\n' | grep -E '^(REMINDER_BACKGROUND|SMS_PROVIDER|SMS_WEBHOOK|NOTIFY_PROVIDER|NOTIFY_WEBHOOK)=' || true)"
echo "$EFF"
echo "$EFF" | grep -qx 'REMINDER_BACKGROUND=0' \
  || { echo "FATAL: REMINDER_BACKGROUND not 0 in process env"; fail_partial_install "failed_sms_verify_reminder"; }
echo "$EFF" | grep -qx 'SMS_PROVIDER=webhook' \
  || { echo "FATAL: SMS_PROVIDER not webhook in process env"; fail_partial_install "failed_sms_verify_provider"; }
echo "$EFF" | grep -qx 'SMS_WEBHOOK=http://127.0.0.1:9/asa-sms-sink' \
  || { echo "FATAL: SMS_WEBHOOK sink missing in process env"; fail_partial_install "failed_sms_verify_webhook"; }
echo "$EFF" | grep -qx 'NOTIFY_PROVIDER=webhook' \
  || { echo "FATAL: NOTIFY_PROVIDER not webhook in process env"; fail_partial_install "failed_sms_verify_notify_provider"; }
echo "$EFF" | grep -qx 'NOTIFY_WEBHOOK=http://127.0.0.1:9/asa-notify-sink' \
  || { echo "FATAL: NOTIFY_WEBHOOK sink missing in process env"; fail_partial_install "failed_sms_verify_notify_webhook"; }
echo "$READY_JSON" | grep -q '"notify_provider"[[:space:]]*:[[:space:]]*"webhook"' \
  || { echo "FATAL: readyz notify_provider is not webhook"; fail_partial_install "failed_sms_verify_readyz"; }
echo "SMS sink verification: OK (no SMS.ir call performed)"

echo "=== write nginx site (previous configs already backed up) ==="
sudo tee "$NGINX_AVAILABLE" >/dev/null <<'NGX'
upstream asa_physio_app {
    server 127.0.0.1:8080;
    keepalive 8;
}
server {
    listen 80;
    listen [::]:80;
    server_name asa-physio.ir www.asa-physio.ir;
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/html;
        default_type "text/plain";
        allow all;
    }
    client_max_body_size 52m;
    location / {
        proxy_pass http://asa_physio_app;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_read_timeout 120s;
    }
}
NGX
sudo ln -sfn "$NGINX_AVAILABLE" "$NGINX_ENABLED"
if [ -e "$NGINX_DEFAULT_ENABLED" ] || [ -L "$NGINX_DEFAULT_ENABLED" ]; then
  sudo rm -f "$NGINX_DEFAULT_ENABLED"
fi
if ! sudo nginx -t; then
  fail_partial_install "failed_nginx_config_test"
fi
if ! sudo systemctl reload nginx; then
  fail_partial_install "failed_nginx_reload"
fi
set_install_phase "nginx"

echo "=== DNS gate + Certbot (both hostnames required unless SKIP) ==="
set_install_phase "certbot"
if [ "${CERTBOT_SKIP_HTTPS:-}" = "1" ]; then
  HTTPS_OK=0
  INSTALL_OUTCOME="partial_http_only"
  echo "SKIP certbot by explicit CERTBOT_SKIP_HTTPS=1."
  echo "PARTIAL INSTALL: HTTP-only by operator choice — full Production HTTPS success is NOT claimed."
  print_manual_restore_and_continue_hints
else
  APEX_OK=0
  WWW_OK=0
  getent hosts asa-physio.ir >/dev/null && APEX_OK=1 || echo "missing DNS: asa-physio.ir"
  getent hosts www.asa-physio.ir >/dev/null && WWW_OK=1 || echo "missing DNS: www.asa-physio.ir"

  if [ "$APEX_OK" != 1 ] || [ "$WWW_OK" != 1 ]; then
    fail_partial_install "failed_https_dns"
  fi

  CERTBOT_ARGS=(--nginx -d asa-physio.ir -d www.asa-physio.ir --non-interactive --agree-tos --redirect)
  if [ -n "${CERTBOT_EMAIL:-}" ]; then
    CERTBOT_ARGS+=(--email "${CERTBOT_EMAIL}")
  elif [ "${CERTBOT_NO_EMAIL:-}" = "1" ]; then
    CERTBOT_ARGS+=(--register-unsafely-without-email)
    echo "WARNING: using --register-unsafely-without-email (explicit CERTBOT_NO_EMAIL=1)"
  else
    echo "FATAL: internal error — cert policy gate should have stopped earlier"
    fail_partial_install "failed_https_internal_cert_policy"
  fi

  if ! sudo certbot "${CERTBOT_ARGS[@]}"; then
    fail_partial_install "failed_https_certbot"
  fi

  if curl -fsS -o /dev/null --connect-timeout 10 "https://asa-physio.ir/" \
    && curl -fsS -o /dev/null --connect-timeout 10 "https://www.asa-physio.ir/"; then
    HTTPS_OK=1
    echo "HTTPS check: OK for asa-physio.ir and www.asa-physio.ir"
  else
    fail_partial_install "failed_https_probe"
  fi

  if ! ensure_certbot_timer_and_dry_run; then
    fail_partial_install "failed_https_renew_timer"
  fi
fi

echo "=== smoke via Host (HTTP) — failures are fatal ==="
# Do NOT use `curl … || echo 000` inside the same substitution: on connect failure
# curl already prints http_code 000 and a failed exit would append another 000 → 000000.
for host in asa-physio.ir www.asa-physio.ir; do
  code="$(curl -sS -o /dev/null -w "%{http_code}" --connect-timeout 5 -H "Host: $host" "http://127.0.0.1/" || true)"
  case "$code" in
    ""|*[!0-9]* ) code="000" ;;
  esac
  echo "$host -> $code"
  case "$code" in
    200|301|302|303|307|308) ;;
    *) fail_partial_install "failed_smoke_http_host_${host}_${code}" ;;
  esac
done

if [ "${CERTBOT_SKIP_HTTPS:-}" != "1" ]; then
  echo "=== smoke HTTPS domains — failures are fatal ==="
  for url in "https://asa-physio.ir/" "https://www.asa-physio.ir/"; do
    if ! curl -fsS -o /dev/null --connect-timeout 10 "$url"; then
      fail_partial_install "failed_smoke_https"
    fi
    echo "OK $url"
  done
fi

echo "=== summary ==="
echo "TARGET_SHA=$TARGET_SHA"
echo "RUN_BACKUP_DIR=$RUN_BACKUP_DIR"
echo "OVERRIDE_ENV=$OVERRIDE_ENV (mode 600)"
echo "INSTALL_PHASE=$INSTALL_PHASE"
if [ -f "${RUN_BACKUP_DIR}/asa.db.was_missing" ]; then
  echo "DB_PRESTATE=missing (explicitly recorded)"
elif [ -f "${RUN_BACKUP_DIR}/asa.db.pre-migrate" ]; then
  echo "DB_PRESTATE=present (Backup API snapshot kept + sha256)"
else
  echo "DB_PRESTATE=unknown (unexpected)"
fi
echo "OWNER_EXISTED_BEFORE=${OWNER_EXISTED_BEFORE:-unknown}"
if [ -f "${RUN_BACKUP_DIR}/owner.seed.state" ]; then
  echo "OWNER_SEED_STATE=$(sudo cat "${RUN_BACKUP_DIR}/owner.seed.state")"
else
  echo "OWNER_SEED_STATE=missing"
fi
echo "HTTPS_OK=$HTTPS_OK  (1=both domains probed OK; 0=not full HTTPS success)"
echo "CERTBOT_TIMER_OK=$CERTBOT_TIMER_OK CERTBOT_RENEW_DRY_RUN_OK=$CERTBOT_RENEW_DRY_RUN_OK"
echo "NON_DB_ROLLBACK_ATTEMPTED=$NON_DB_ROLLBACK_ATTEMPTED NON_DB_ROLLBACK_VERIFIED=$NON_DB_ROLLBACK_VERIFIED"
echo "CERTBOT_TIMER_RESTORE_ATTEMPTED=$CERTBOT_TIMER_RESTORE_ATTEMPTED CERTBOT_TIMER_RESTORE_VERIFIED=$CERTBOT_TIMER_RESTORE_VERIFIED"
echo "UPLOAD_DIR=$UPLOAD_DIR_VALIDATED (validated under APP_ROOT)"
echo "Real SMS blocked via post-.env EnvironmentFile sink; REMINDER_BACKGROUND=0 verified."
echo ".env.production was NOT modified or printed."
echo "Admin password was not written to logs, RESTORE.txt, or process argv."
echo "LIMITATION: ops.seed still receives ADMIN_PASSWORD via child environment (not FD/stdin)."

if [ "${CERTBOT_SKIP_HTTPS:-}" = "1" ]; then
  INSTALL_OUTCOME="partial_http_only"
  set_install_phase "done_partial"
  mark_install_incomplete
  echo "RESULT: PARTIAL — HTTP-only (CERTBOT_SKIP_HTTPS=1). Production HTTPS success NOT claimed."
  print_manual_restore_and_continue_hints
  exit 2
fi

if [ "$HTTPS_OK" != 1 ] || [ "$CERTBOT_TIMER_OK" != 1 ] || [ "$CERTBOT_RENEW_DRY_RUN_OK" != 1 ]; then
  fail_partial_install "failed_https_final_gate"
fi

INSTALL_OUTCOME="success"
set_install_phase "done"
mark_install_complete
echo "RESULT: HTTP app + HTTPS both domains OK; certbot.timer + renew dry-run OK; SMS sink verified."
echo "Await operator confirmation before any further production changes."
