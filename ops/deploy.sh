#!/usr/bin/env bash
# Layer 05 — one command, reproducible, with a release trail for rollback.
#   ops/deploy.sh [staging|production]
set -euo pipefail
cd "$(dirname "$0")/.."
ENVNAME="${1:-staging}"
REL="$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
RELDIR="releases/$REL"

echo "▸ 1/7 preflight"
[ -f ".env.$ENVNAME" ] || { echo "missing .env.$ENVNAME (copy .env.example)" >&2; exit 1; }
set -a; . "./.env.$ENVNAME"; set +a
: "${SECRET_KEY:?SECRET_KEY must be set}"

echo "▸ 2/7 checks"
ruff check api tests build_static.py
ruff format --check api tests build_static.py
# run the suite with the deployment environment stripped: tests must never see a real DB
env -u DB_PATH -u BACKUP_DIR -u NOTIFY_PROVIDER -u NOTIFY_WEBHOOK -u NOTIFY_FILE \
    -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHAT_ID -u ALERT_WEBHOOK -u AI_API_KEY \
    -u SECRET_KEY -u APP_ENV -u STATIC_DIR \
    python3 -m pytest -q

echo "▸ 3/7 backup before touching anything"
ops/backup.sh >/dev/null && echo "   backup taken"

echo "▸ 4/7 build static (fingerprinted)"
python3 work/build_pages.py >/dev/null
python3 build_static.py

echo "▸ 5/7 migrate (forward only)"
python3 -m api.db

echo "▸ 6/7 snapshot release $REL"
mkdir -p "$RELDIR"
cp -r api migrations public build_static.py "$RELDIR"/ 2>/dev/null || true
git rev-parse HEAD > "$RELDIR/GIT_SHA" 2>/dev/null || echo nogit > "$RELDIR/GIT_SHA"
echo "$REL" > CURRENT_RELEASE
ls -1t releases 2>/dev/null | tail -n +6 | xargs -r -I{} rm -rf "releases/{}"

echo "▸ 7/7 restart"
ops/serve.sh restart "$ENVNAME"
sleep 2
curl -fsS "http://127.0.0.1:${PORT:-8080}/api/readyz" >/dev/null \
  && echo "✔ deployed $REL to $ENVNAME" \
  || { echo "✘ readyz failed — run: ops/rollback.sh" >&2; exit 1; }
