#!/usr/bin/env bash
# start | stop | restart | status
set -euo pipefail
cd "$(dirname "$0")/.."
CMD="${1:-start}"; ENVNAME="${2:-staging}"
PIDF="data/$ENVNAME.pid"; LOGF="data/$ENVNAME.log"
mkdir -p data
[ -f ".env.$ENVNAME" ] && { set -a; . "./.env.$ENVNAME"; set +a; }
PORT="${PORT:-8080}"; WORKERS="${WORKERS:-1}"

stop() {
  if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
    kill "$(cat "$PIDF")"; sleep 1; fi
  rm -f "$PIDF"
}
case "$CMD" in
  stop) stop; echo "stopped";;
  status) [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null && echo "running pid=$(cat "$PIDF")" || echo "not running";;
  start|restart)
    [ "$CMD" = restart ] && stop
    nohup python3 -m uvicorn api.main:app --host 0.0.0.0 --port "$PORT" \
      --workers "$WORKERS" --no-access-log >> "$LOGF" 2>&1 &
    echo $! > "$PIDF"
    echo "started $ENVNAME on :$PORT pid=$(cat "$PIDF")";;
  *) echo "usage: ops/serve.sh {start|stop|restart|status} [env]" >&2; exit 1;;
esac
