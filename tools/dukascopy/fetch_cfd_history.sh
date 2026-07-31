#!/usr/bin/env bash
#
# fetch_cfd_history.sh — run the Dukascopy CFD fetcher in the background.
#
# The full 10-year pull takes hours, so this nohups the Python orchestrator so
# it survives terminal/SSH disconnects, writing to a logfile with a pidfile.
#
# Usage (any args are passed straight to the fetcher):
#   ./fetch_cfd_history.sh                                  # full default 10y, all 10
#   ./fetch_cfd_history.sh --instruments EURUSD --month 2020-06   # small verify slice
#   ./fetch_cfd_history.sh --year 2021
#
# Control:
#   tail -f logs/dukascopy_fetch.log      # watch progress
#   ./fetch_cfd_history.sh --status       # is it running?
#   ./fetch_cfd_history.sh --stop         # stop a running fetch
#   ./fetch_cfd_history.sh --summary      # print stored candle summary (foreground)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG_DIR="$REPO_ROOT/logs"
LOG_FILE="$LOG_DIR/dukascopy_fetch.log"
PID_FILE="$LOG_DIR/dukascopy_fetch.pid"

mkdir -p "$LOG_DIR"

# Pick a python: project venv first, then python3.
if [ -x "$REPO_ROOT/venv/bin/python" ]; then
  PY="$REPO_ROOT/venv/bin/python"
elif [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  PY="$REPO_ROOT/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

is_running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

case "${1:-}" in
  --status)
    if is_running; then
      echo "RUNNING (pid $(cat "$PID_FILE")). Log: $LOG_FILE"
    else
      echo "NOT running."
    fi
    exit 0
    ;;
  --stop)
    if is_running; then
      PID="$(cat "$PID_FILE")"
      kill "$PID" && echo "Stopped pid $PID."
      rm -f "$PID_FILE"
    else
      echo "NOT running."
    fi
    exit 0
    ;;
  --summary)
    # Run in the foreground — it's quick and just prints the DB summary.
    cd "$REPO_ROOT"
    exec "$PY" -m app.backtest.fetch_dukascopy --summary
    ;;
esac

if is_running; then
  echo "Already running (pid $(cat "$PID_FILE")). Use --stop first, or --status."
  exit 1
fi

cd "$REPO_ROOT"
echo "Starting Dukascopy fetch in background..."
echo "  python:  $PY"
echo "  args:    $*"
echo "  log:     $LOG_FILE"

nohup "$PY" -m app.backtest.fetch_dukascopy "$@" >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

echo "  pid:     $(cat "$PID_FILE")"
echo "Watch it:  tail -f $LOG_FILE"
