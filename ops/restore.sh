#!/usr/bin/env bash
# Restore a verified backup bundle into an experimental directory by default.
# Usage:
#   ops/restore.sh <bundle-dir> [dest_dir]
#   ops/restore.sh <bundle-dir> --promote
#   ops/restore.sh <asa-*.db.gz> <target.db> [--confirm-live-overwrite]
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="${1:?usage: ops/restore.sh <bundle-dir|asa-*.db.gz> [dest_dir|--promote]}"
shift || true

if [[ -f "$SRC" && "$SRC" == *.db.gz ]]; then
  DEST="${1:-}"
  shift || true
  CONFIRM=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --confirm-live-overwrite) CONFIRM+=(--confirm-live-overwrite); shift ;;
      *) echo "unknown legacy flag: $1" >&2; exit 2 ;;
    esac
  done
  if [[ -z "$DEST" ]]; then
    echo "legacy restore requires an explicit target.db path" >&2
    echo "refusing default live overwrite; pass dest and --confirm-live-overwrite if intentional" >&2
    exit 2
  fi
  exec python3 -m ops.backup_core restore-legacy "$SRC" "$DEST" "${CONFIRM[@]}"
fi

ARGS=(restore "$SRC")
PROMOTE=0
DEST=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --promote) PROMOTE=1; shift ;;
    --live-db) ARGS+=(--live-db "$2"); shift 2 ;;
    --live-uploads) ARGS+=(--live-uploads "$2"); shift 2 ;;
    -*)
      echo "unknown flag: $1" >&2
      exit 2
      ;;
    *)
      if [[ -z "$DEST" ]]; then DEST="$1"; shift; else echo "unexpected arg: $1" >&2; exit 2; fi
      ;;
  esac
done
[[ -n "$DEST" ]] && ARGS+=("$DEST")
[[ "$PROMOTE" -eq 1 ]] && ARGS+=(--promote)
[[ -n "${DB_PATH:-}" ]] && ARGS+=(--live-db "$DB_PATH")
[[ -n "${UPLOAD_DIR:-}" ]] && ARGS+=(--live-uploads "$UPLOAD_DIR")

exec python3 -m ops.backup_core "${ARGS[@]}"
