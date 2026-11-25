#!/usr/bin/env bash
# Verified backup: SQLite (online) + uploads + manifest + SHA256.
# Partial bundles are never marked OK. Secrets are never printed.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -m ops.backup_core backup \
  ${DB_PATH:+--db "$DB_PATH"} \
  ${UPLOAD_DIR:+--uploads "$UPLOAD_DIR"} \
  ${BACKUP_DIR:+--backup-dir "$BACKUP_DIR"} \
  ${BACKUP_KEEP:+--keep "$BACKUP_KEEP"} \
  ${BACKUP_INCLUDE_ENV:+--include-env "$BACKUP_INCLUDE_ENV"}
