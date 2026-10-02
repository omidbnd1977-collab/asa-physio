#!/usr/bin/env bash
# Layer 13 — restore a backup into a target database and verify it.
# usage: ops/restore.sh <backup.db.gz> [target.db]
set -euo pipefail
cd "$(dirname "$0")/.."
SRC="${1:?usage: ops/restore.sh <backup.db.gz> [target.db]}"
DST="${2:-data/asa.db}"
[ -f "$SRC" ] || { echo "no such backup: $SRC" >&2; exit 1; }
START=$(date +%s)

if [ -f "$DST" ]; then
  SAFE="$DST.before-restore-$(date -u +%Y%m%dT%H%M%SZ)"
  cp "$DST" "$SAFE"; echo "current db parked at $SAFE"
fi
mkdir -p "$(dirname "$DST")"
gunzip -c "$SRC" > "$DST"
rm -f "$DST-wal" "$DST-shm"

python3 - "$DST" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
fk = c.execute("PRAGMA foreign_key_check").fetchall()
assert not fk, f"foreign key violations: {fk}"
t = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
b = c.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
m = c.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
print(f"restored: {len(t)} tables, {b} bookings, {m} migrations applied")
PY
echo "restore completed in $(( $(date +%s) - START ))s"
