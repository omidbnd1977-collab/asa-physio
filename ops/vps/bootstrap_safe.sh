#!/usr/bin/env bash
# ASA Physio — safe VPS bootstrap to exact SHA (no force-push, no .env overwrite, no real SMS).
# Run on the VPS as asaadmin (with sudo for nginx/systemd/certbot):
#   bash /opt/asa-physio/ops/vps/bootstrap_safe.sh
set -euo pipefail

# Pin to the intended deploy commit.
# Currently main tip (c4af859). After this integrate branch is committed/merged,
# bump TARGET_SHA to that commit before running on the VPS (migrations 0016+0017 + public/).
TARGET_SHA="c4af859e22ee9d6795bc03d65e82b72e8b67b2dc"
APP_ROOT="/opt/asa-physio"
BACKUP_TAR="/opt/asa-physio-backups/asa-before-update-20261113-194450.tar.gz"
BACKUP_SHA256="857f0026f7ebbf6eb03096fea14dc925cffdb45f09d1c79d01d13ce1e37d4cbe"
SERVICE_UNIT_SRC="$APP_ROOT/ops/vps/asa-physio.service"
NGINX_SRC="$APP_ROOT/ops/vps/nginx-asa-physio.conf"
TS="$(date -u +%Y%m%d-%H%M%S)"

echo "=== 0) preflight ==="
id
[ "$(id -un)" = "asaadmin" ] || echo "WARN: expected user asaadmin (continuing)"
[ -d "$APP_ROOT" ] || { echo "missing $APP_ROOT"; exit 1; }
cd "$APP_ROOT"

echo "=== 1) verify existing backup (do not delete) ==="
if [ -f "$BACKUP_TAR" ]; then
  echo "$BACKUP_SHA256  $BACKUP_TAR" | sha256sum -c -
else
  echo "WARN: expected backup tar missing: $BACKUP_TAR"
fi

echo "=== 2) fresh code+data safety backup (before git move) ==="
sudo mkdir -p /opt/asa-physio-backups
SAFE_TAR="/opt/asa-physio-backups/asa-pre-bootstrap-${TS}.tar.gz"
sudo tar -czf "$SAFE_TAR" \
  --exclude='.venv' \
  --exclude='__pycache__' \
  -C /opt asa-physio
echo "wrote $SAFE_TAR"
ls -lh "$SAFE_TAR"

echo "=== 3) preserve .env.production + data ==="
[ -f "$APP_ROOT/.env.production" ] || { echo "FATAL: .env.production missing — stop"; exit 1; }
# Never overwrite/delete .env.production
cp -a "$APP_ROOT/.env.production" "/opt/asa-physio-backups/env.production.${TS}.bak"
mkdir -p "$APP_ROOT/data/uploads" "$APP_ROOT/data/backups"
if [ -f "$APP_ROOT/data/asa.db" ]; then
  cp -a "$APP_ROOT/data/asa.db" "$APP_ROOT/data/backups/asa.db.pre-migrate-${TS}"
  echo "DB backup: data/backups/asa.db.pre-migrate-${TS}"
else
  echo "DB not present yet (will be created on migrate/start)"
fi

echo "=== 4) fetch & checkout exact SHA (no force-push) ==="
git remote -v
git fetch origin
git rev-parse --verify "${TARGET_SHA}^{commit}" >/dev/null
# Keep local untracked env/data; reset tracked tree to target
git checkout -B main "$TARGET_SHA"
git rev-parse HEAD
test "$(git rev-parse HEAD)" = "$TARGET_SHA"

echo "=== 5) venv + deps (reuse existing if healthy) ==="
if [ ! -x "$APP_ROOT/.venv/bin/python" ]; then
  python3 -m venv "$APP_ROOT/.venv"
fi
# shellcheck disable=SC1091
source "$APP_ROOT/.venv/bin/activate"
pip install -q -r requirements.txt

echo "=== 6) migration status (read-only probe) then apply forward-only ==="
# Load env for paths only; do not print secrets
set -a
# shellcheck disable=SC1091
source "$APP_ROOT/.env.production"
set +a
# Force no background SMS during this bootstrap session
export REMINDER_BACKGROUND=0
# Probe applied migrations without printing env
python3 - <<'PY'
from pathlib import Path
import sqlite3, os
db = Path(os.environ.get("DB_PATH", "data/asa.db"))
print("DB_PATH=", db)
if not db.exists():
    print("schema: (empty — no db file)")
else:
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchall()
        if not rows:
            print("schema_migrations: missing (fresh or pre-migration db)")
        else:
            applied = [r[0] for r in con.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()]
            print("applied:", ",".join(applied) if applied else "(none)")
    finally:
        con.close()
migs = sorted(p.name for p in Path("migrations").glob("*.sql"))
print("on_disk:", ",".join(migs))
PY
python3 -m api.db
python3 - <<'PY'
from pathlib import Path
import sqlite3, os
db = Path(os.environ.get("DB_PATH", "data/asa.db"))
con = sqlite3.connect(db)
try:
    applied = [r[0] for r in con.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()]
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]
    print("FINAL applied migrations:", ",".join(applied))
    print("FINAL tables:", ",".join(tables))
finally:
    con.close()
PY

echo "=== 6b) static site build (site/ → public/ with hashed assets) ==="
# Same steps as make build / ops/deploy.sh — required so privacy/terms and CSS land in public/
python3 work/build_pages.py
python3 build_static.py
test -f "$APP_ROOT/public/index.html" || { echo "FATAL: public/index.html missing after build"; exit 1; }

echo "=== 7) install systemd unit (webhook sink + no reminder loop; no SMS.ir) ==="
sudo cp "$SERVICE_UNIT_SRC" /etc/systemd/system/asa-physio.service
sudo systemctl daemon-reload
sudo systemctl enable asa-physio.service
sudo systemctl restart asa-physio.service
sleep 2
sudo systemctl --no-pager --full status asa-physio.service | head -n 40
curl -fsS "http://127.0.0.1:8080/api/healthz" && echo
curl -fsS "http://127.0.0.1:8080/api/readyz" && echo
curl -fsS -o /dev/null -w "index %{http_code}\n" "http://127.0.0.1:8080/"
curl -fsS -o /dev/null -w "css  %{http_code}\n" "http://127.0.0.1:8080/style.41e8105be2.css" || true

echo "=== 8) nginx site (HTTP first; certbot next) ==="
sudo cp "$NGINX_SRC" /etc/nginx/sites-available/asa-physio
sudo ln -sfn /etc/nginx/sites-available/asa-physio /etc/nginx/sites-enabled/asa-physio
# Disable default welcome if present
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx

echo "=== 9) Let's Encrypt (HTTP-01) — apex + www ==="
# Requires BOTH hostnames to resolve to this server before running.
echo "DNS check:"
getent hosts asa-physio.ir || echo "MISSING A/AAAA for asa-physio.ir — fix DNS before certbot"
getent hosts www.asa-physio.ir || echo "MISSING A/AAAA for www.asa-physio.ir"
if getent hosts asa-physio.ir >/dev/null && getent hosts www.asa-physio.ir >/dev/null; then
  sudo certbot --nginx -d asa-physio.ir -d www.asa-physio.ir --non-interactive --agree-tos \
    --redirect --email "${CERTBOT_EMAIL:-admin@asa-physio.ir}" || {
      echo "certbot failed — HTTP site may still work; fix DNS/email and re-run certbot"
    }
  sudo systemctl list-timers 'certbot*' --no-pager || true
else
  echo "SKIP certbot until apex+www both resolve here."
fi

echo "=== 10) external smoke (local via Host header) ==="
for host in asa-physio.ir www.asa-physio.ir; do
  curl -fsS -o /dev/null -w "$host http %{http_code}\n" -H "Host: $host" "http://127.0.0.1/" || true
done

echo "=== DONE ==="
echo "Public site should proxy to uvicorn. Real SMS disabled via systemd webhook-sink overrides."
echo "When SMS patterns are approved: edit /etc/systemd/system/asa-physio.service"
echo "  remove SMS_/NOTIFY_ webhook overrides (keep REMINDER_BACKGROUND=0 until ready),"
echo "  then: sudo systemctl daemon-reload && sudo systemctl restart asa-physio"
echo "Do NOT send test SMS to patients/admin until patterns are confirmed."
