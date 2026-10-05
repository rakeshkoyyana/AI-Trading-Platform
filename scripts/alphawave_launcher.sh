#!/usr/bin/env bash
# One-click AlphaWave: update to the latest main, (re)start the scheduler + dashboard if needed, open the dashboard.
#   - nothing running            -> start both
#   - running, code unchanged    -> just open the dashboard
#   - running, new code pulled   -> restart both so new features show up
# Stopping or restarting only affects the local processes; it never touches positions or orders at the broker.
# Keep the Mac awake yourself (e.g. `caffeinate -dims`); this script does not.
set -u

main() {
  REPO="${ALPHAWAVE_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  PORT="${ALPHAWAVE_PORT:-8501}"
  URL="http://localhost:${PORT}"
  RUN_DIR="$REPO/data/run"
  LOG_DIR="$REPO/logs"

  # Apps started from Finder get a bare PATH; add the usual Homebrew/user locations.
  export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
  cd "$REPO" || { echo "Repo not found: $REPO" >&2; return 1; }
  mkdir -p "$RUN_DIR" "$LOG_DIR"

  notify() {
    command -v osascript >/dev/null 2>&1 &&
      osascript -e "display notification \"$1\" with title \"AlphaWave\"" >/dev/null 2>&1
    echo "$1"
  }
  alive() { [ -f "$1" ] && kill -0 "$(cat "$1" 2>/dev/null)" 2>/dev/null; }
  port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; }
  stop_proc() {  # stop_proc <name>  (local process only)
    local pf="$RUN_DIR/$1.pid" pid
    if alive "$pf"; then
      pid="$(cat "$pf")"; kill "$pid" 2>/dev/null
      for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
      kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$pf"
  }

  # --- pull the latest main (only when it is safe: on main, no local edits, fast-forward) ----
  pulled=""
  if [ -d .git ] && command -v git >/dev/null 2>&1 && [ "${ALPHAWAVE_NO_PULL:-0}" != "1" ]; then
    if [ "$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" = "main" ] &&
       [ -z "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
      before="$(git rev-parse HEAD 2>/dev/null)"
      GIT_TERMINAL_PROMPT=0 git pull --ff-only -q >"$LOG_DIR/update.log" 2>&1 &
      gp=$!
      for _ in $(seq 1 40); do kill -0 "$gp" 2>/dev/null || break; sleep 0.5; done
      kill -0 "$gp" 2>/dev/null && kill "$gp" 2>/dev/null
      [ "$(git rev-parse HEAD 2>/dev/null)" != "$before" ] && pulled="yes"
    else
      echo "Skipping update: not on a clean main branch."
    fi
  fi

  # --- python environment ------------------------------------------------------------
  PY=""
  for p in "$REPO/venv/bin/python" "$REPO/.venv/bin/python"; do [ -x "$p" ] && { PY="$p"; break; }; done
  [ -z "$PY" ] && PY="$(command -v python3)"
  # Install new dependencies only when requirements.txt changed.
  if [ -f requirements.txt ] && [ -n "$PY" ]; then
    rh="$(cksum < requirements.txt)"
    if [ "$rh" != "$(cat "$RUN_DIR/requirements.cksum" 2>/dev/null)" ] && "$PY" -m pip --version >/dev/null 2>&1; then
      "$PY" -m pip install -q -r requirements.txt >>"$LOG_DIR/update.log" 2>&1 && echo "$rh" >"$RUN_DIR/requirements.cksum"
    fi
  fi
  if [ -z "$PY" ] || ! "$PY" -c "import streamlit, apscheduler" 2>/dev/null; then
    notify "Python environment not ready. In Terminal run: cd \"$REPO\" && python3 -m venv venv && venv/bin/pip install -r requirements.txt"
    return 1
  fi

  # --- restart if the running code is older than the checked-out code -------------------
  version="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  restarted=""
  if { alive "$RUN_DIR/scheduler.pid" || alive "$RUN_DIR/dashboard.pid"; } &&
     [ "$(cat "$RUN_DIR/version" 2>/dev/null)" != "$version" ]; then
    stop_proc scheduler; stop_proc dashboard
    for _ in $(seq 1 20); do port_open || break; sleep 0.5; done
    restarted="yes"
  fi
  echo "$version" >"$RUN_DIR/version"

  started=""
  if alive "$RUN_DIR/scheduler.pid"; then
    echo "Scheduler already running (pid $(cat "$RUN_DIR/scheduler.pid"))."
  else
    nohup "$PY" -m src.scheduler.run_loop >>"$LOG_DIR/scheduler.log" 2>&1 &
    echo $! >"$RUN_DIR/scheduler.pid"
    started="scheduler"
  fi
  if alive "$RUN_DIR/dashboard.pid" || port_open; then
    echo "Dashboard already running on port $PORT."
  else
    nohup "$PY" -m streamlit run src/dashboard/app.py \
      --server.port "$PORT" --server.headless true --browser.gatherUsageStats false \
      >>"$LOG_DIR/dashboard.log" 2>&1 &
    echo $! >"$RUN_DIR/dashboard.pid"
    started="${started:+$started + }dashboard"
  fi

  # --- wait for the dashboard, then open it ------------------------------------------------
  for _ in $(seq 1 60); do port_open && break; sleep 0.5; done
  if ! port_open; then notify "Dashboard did not start. See $LOG_DIR/dashboard.log"; return 1; fi
  if command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1; fi

  if [ -n "$restarted" ]; then notify "Updated${pulled:+ to the latest version} and restarted. Dashboard: $URL"
  elif [ -n "$started" ]; then notify "Started $started${pulled:+ (latest version)}. Dashboard: $URL"
  else notify "Already running the latest version. Opened $URL"; fi
}

main "$@"
exit $?
