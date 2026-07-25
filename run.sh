#!/usr/bin/env bash
# GenSight launcher / service manager.
#
#   ./run.sh                foreground run (dev, Ctrl+C to stop)
#   ./run.sh start          background start (pid/log under data/)
#   ./run.sh stop           graceful stop (TERM, then KILL after 10s)
#   ./run.sh restart        stop + start
#   ./run.sh reload         graceful restart (uvicorn has no hot-reload signal)
#   ./run.sh status         running state + health check
#
# Environment: HOST (default 127.0.0.1), PORT (default 8090)
set -euo pipefail
cd "$(dirname "$0")"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8090}"
PID_FILE="data/gensight.pid"
LOG_FILE="data/gensight.log"

ensure_venv() {
  if [ ! -d .venv ]; then
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt
  fi
}

pid() { cat "$PID_FILE" 2>/dev/null || true; }

is_running() {
  [ -f "$PID_FILE" ] && kill -0 "$(pid)" 2>/dev/null
}

start() {
  if is_running; then
    echo "already running (pid $(pid))"
    return 0
  fi
  ensure_venv
  mkdir -p data
  nohup .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" \
    >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  # Wait until the port answers (max ~10s)
  for _ in $(seq 1 20); do
    if curl -sf -o /dev/null "http://$HOST:$PORT/"; then
      echo "started: http://$HOST:$PORT (pid $(pid), log: $LOG_FILE)"
      return 0
    fi
    if ! is_running; then break; fi
    sleep 0.5
  done
  echo "failed to start — last log lines:" >&2
  tail -n 10 "$LOG_FILE" >&2 || true
  rm -f "$PID_FILE"
  return 1
}

stop() {
  if ! is_running; then
    echo "not running"
    rm -f "$PID_FILE"
    return 0
  fi
  local p
  p="$(pid)"
  kill "$p"
  for _ in $(seq 1 20); do
    kill -0 "$p" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$p" 2>/dev/null; then
    echo "still alive after 10s — sending SIGKILL"
    kill -9 "$p" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo "stopped (pid $p)"
}

status() {
  if is_running; then
    echo "running (pid $(pid)) — http://$HOST:$PORT"
    if curl -sf -o /dev/null "http://$HOST:$PORT/"; then
      echo "health: OK"
    else
      echo "health: NOT RESPONDING (process alive but port $PORT not answering)"
      return 1
    fi
  else
    rm -f "$PID_FILE"
    echo "stopped"
    return 3   # LSB convention: 3 = not running
  fi
}

case "${1:-run}" in
  run)
    ensure_venv
    shift || true
    exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"
    ;;
  start)   start ;;
  stop)    stop ;;
  restart) stop; start ;;
  reload)
    # uvicorn cannot reload code via signal; do a graceful restart.
    echo "reload = graceful restart (uvicorn has no hot-reload signal)"
    stop
    start
    ;;
  status)  status ;;
  *)
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
