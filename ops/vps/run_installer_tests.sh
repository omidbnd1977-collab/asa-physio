#!/usr/bin/env bash
# Run all local installer regression tests (no VPS).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"
export PATH="/usr/bin:/bin:/usr/local/bin:${PATH:-}"

bash -n "$DIR/INSTALL_ON_VPS.sh"
echo "PASS: bash -n INSTALL_ON_VPS.sh"

bash "$DIR/test_backup_dir_exclusive.sh"
bash "$DIR/test_restore_notes_root_only.sh"
bash "$DIR/test_healthz_poll_wait.sh"

echo "ALL INSTALLER TESTS: PASS"
