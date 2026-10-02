#!/usr/bin/env bash
# Layer 05 — the written, exercised way back.
#   ops/rollback.sh            -> previous release
#   ops/rollback.sh <release>  -> a specific release
set -euo pipefail
cd "$(dirname "$0")/.."
ENVNAME="${ENVNAME:-staging}"
CUR="$(cat CURRENT_RELEASE 2>/dev/null || echo '')"
TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  TARGET="$(ls -1t releases 2>/dev/null | grep -v "^$CUR$" | head -1 || true)"
fi
[ -n "$TARGET" ] && [ -d "releases/$TARGET" ] || { echo "no release to roll back to" >&2; exit 1; }
echo "rolling back: $CUR -> $TARGET"
cp -r "releases/$TARGET/api" "releases/$TARGET/migrations" "releases/$TARGET/public" . 
echo "$TARGET" > CURRENT_RELEASE
ops/serve.sh restart "$ENVNAME"
sleep 2
curl -fsS "http://127.0.0.1:${PORT:-8080}/api/readyz" >/dev/null \
  && echo "✔ rolled back to $TARGET" || { echo "✘ still unhealthy" >&2; exit 1; }
