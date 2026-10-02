#!/usr/bin/env bash
# Layer 13 — automated, verified backup. Safe to run while the app is serving (WAL + .backup).
set -euo pipefail
cd "$(dirname "$0")/.."
DB="${DB_PATH:-data/asa.db}"
DIR="${BACKUP_DIR:-data/backups}"
KEEP="${BACKUP_KEEP:-14}"
mkdir -p "$DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="$DIR/.tmp-$STAMP.db"
OUT="$DIR/asa-$STAMP.db.gz"

python3 - "$DB" "$TMP" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src); d = sqlite3.connect(dst)
with d: s.backup(d)            # consistent online snapshot
d.execute("PRAGMA integrity_check")
row = d.execute("PRAGMA integrity_check").fetchone()
assert row and row[0] == "ok", f"integrity_check failed: {row}"
n = d.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
print(f"snapshot ok, bookings={n}")
s.close(); d.close()
PY

gzip -9 -c "$TMP" > "$OUT"
rm -f "$TMP"
echo "backup: $OUT ($(du -h "$OUT" | cut -f1))"

# retention
ls -1t "$DIR"/asa-*.db.gz 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f
echo "kept: $(ls -1 "$DIR"/asa-*.db.gz 2>/dev/null | wc -l) backups"
