#!/usr/bin/env bash
# Start AlphaWave (scheduler + dashboard) in the background and open the dashboard.
# Safe to run repeatedly: anything already running is left alone.
# Used by the desktop app made with scripts/install_desktop_app.sh, but works from a terminal too.
set -u

REPO="${ALPHAWAVE_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PORT="${ALPHAWAVE_PORT:-8501}"
URL="http://localhost:${PORT}"
RUN_DIR="$REPO/data/run"
LOG_DIR="$REPO/logs"

# Apps started from Finder get a bare PATH; add the usual Homebrew/user locations.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
cd "$REPO" || { echo "Repo not found: $REPO" >&2; exit 1; }
mkdir -p "$RUN_DIR" "$LOG_DIR"

notify() {  # notify "message"  (macOS banner; silently skipped elsewhere)
  command -v osascript >/dev/null 2>&1 &&
    osascript -e "display notification \"$1\" with title \"AlphaWave\"" >/dev/null 2>&1
  echo "$1"
}

pick_python() {
  for p in "$REPO/venv/bin/python" "$REPO/.venv/bin/python"; do
    [ -x "$p" ] && { echo "$p"; return; }
  done
  command -v python3
}

alive() {  # alive <pidfile>
  [ -f "$1" ] && kill -0 "$(cat "$1" 2>/dev/null)" 2>/dev/null
}

port_open() {
  (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null
}

PY="$(pick_python)"
if [ -z "$PY" ] || ! "$PY" -c "import streamlit, apscheduler" 2>/dev/null; then
  msg="Python environment not ready. In Terminal run: cd \"$REPO\" && python3 -m venv venv && venv/bin/pip install -r requirements.txt"
  notify "$msg"
  exit 1
fi

started=""

# --- scheduler -------------------------------------------------------------
if alive "$RUN_DIR/scheduler.pid"; then
  echo "Scheduler already running (pid $(cat "$RUN_DIR/scheduler.pid"))."
else
  nohup "$PY" -m src.scheduler.run_loop >>"$LOG_DIR/scheduler.log" 2>&1 &
  echo $! >"$RUN_DIR/scheduler.pid"
  # Keep the Mac awake for as long as the scheduler lives.
  if command -v caffeinate >/dev/null 2>&1; then
    nohup caffeinate -dims -w "$(cat "$RUN_DIR/scheduler.pid")" >/dev/null 2>&1 &
  fi
  started="scheduler"
fi

# --- dashboard -------------------------------------------------------------
if alive "$RUN_DIR/dashboard.pid" || port_open; then
  echo "Dashboard already running on port $PORT."
else
  nohup "$PY" -m streamlit run src/dashboard/app.py \
    --server.port "$PORT" --server.headless true --browser.gatherUsageStats false \
    >>"$LOG_DIR/dashboard.log" 2>&1 &
  echo $! >"$RUN_DIR/dashboard.pid"
  started="${started:+$started + }dashboard"
fi

# --- wait for the dashboard, then open it ----------------------------------
for _ in $(seq 1 60); do
  port_open && break
  sleep 0.5
done

if port_open; then
  if command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1
  fi
  if [ -n "$started" ]; then notify "Started $started. Dashboard: $URL"
  else notify "Already running. Opened $URL"; fi
else
  notify "Dashboard did not start. See $LOG_DIR/dashboard.log"
  exit 1
fi
