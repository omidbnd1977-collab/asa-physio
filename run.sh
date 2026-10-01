#!/usr/bin/env bash
# آسا فیزیو — اجرای سرور (waitress) یا تست‌ها
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="/home/user/pylibs:$(pwd)"

case "${1:-serve}" in
  serve)
    exec python3 -c "
from waitress import serve
from api.app import create_app
app = create_app()
print('ASA-READY on http://0.0.0.0:' + str(__import__('os').environ.get('PORT', 8080)), flush=True)
serve(app, host='0.0.0.0', port=int(__import__('os').environ.get('PORT', 8080)), threads=8)
"
    ;;
  test)  exec python3 scripts/check.py ;;
  sms)   shift; exec curl -sS -X POST http://127.0.0.1:${PORT:-8080}/api/sms/inbound \
            -H "Content-Type: application/json" \
            -H "X-SMS-Secret: ${ASA_INBOUND_SECRET:-asa-dev-inbound-secret}" \
            -d "{\"phone\":\"$1\",\"body\":\"$2\"}" ;;
  *)     echo "usage: $0 [serve|test|sms <phone> <body>]"; exit 2 ;;
esac
