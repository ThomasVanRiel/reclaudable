#!/bin/sh
# rm-llm watcher control: start | stop | status | restart
# Manual launch (no systemd/docker). Re-run `start` after a reboot.
# The watcher itself writes timestamped logs to logs/watcher.log; this script
# sends any pre-logging crash output to logs/watcher.out.
set -e
DIR="/home/you/source/rm-llm"
cd "$DIR"
PYTHON="$DIR/.venv/bin/python3"
PATTERN="[w]atcher.py"          # bracket avoids matching the pgrep itself

running() { pgrep -fa "$PATTERN" 2>/dev/null; }

case "${1:-status}" in
  start)
    if running >/dev/null; then
      echo "already running:"; running; exit 0
    fi
    mkdir -p logs
    nohup "$PYTHON" -u watcher.py --interval 8 >>logs/watcher.out 2>&1 &
    echo "started (pid $!). live log: $DIR/logs/watcher.log"
    ;;
  stop)
    if running >/dev/null; then pkill -f "$PATTERN" && echo "stopped."; else echo "not running."; fi
    ;;
  restart)
    "$0" stop 2>/dev/null || true
    exec "$0" start
    ;;
  status)
    if running >/dev/null; then echo "RUNNING:"; running; else echo "not running."; fi
    ;;
  *)
    echo "usage: $0 {start|stop|status|restart}"; exit 1 ;;
esac
