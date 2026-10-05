#!/usr/bin/env bash
# One-click AlphaWave: update to the latest main, (re)start the scheduler + dashboard if needed, open the dashboard.
#   - nothing running            -> start both
#   - running, code unchanged    -> asks: Open dashboard / Restart / Stop
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
  wait_dead() {  # wait_dead <pid> <seconds>: wait for exit, then force-kill (local process only)
    local pid="$1" n=$(( $2 * 5 ))
    for _ in $(seq 1 "$n"); do kill -0 "$pid" 2>/dev/null || return 0; sleep 0.2; done
    kill -9 "$pid" 2>/dev/null
  }
  term_proc() {  # term_proc <name>: ask the process to stop; prints its old pid (empty if not running)
    local pf="$RUN_DIR/$1.pid"
    if alive "$pf"; then cat "$pf"; kill "$(cat "$pf")" 2>/dev/null; fi
    rm -f "$pf"
  }
  start_scheduler() {
    nohup "$PY" -m src.scheduler.run_loop >>"$LOG_DIR/scheduler.log" 2>&1 &
    echo $! >"$RUN_DIR/scheduler.pid"
  }

  # --- pull the latest main (only when it is safe: on main, no local edits, fast-forward) ----
  pulled=""
  if [ -d .git ] && command -v git >/dev/null 2>&1 && [ "${ALPHAWAVE_NO_PULL:-0}" != "1" ]; then
    if [ "$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" = "main" ] &&
       [ -z "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
      before="$(git rev-parse HEAD 2>/dev/null)"
      GIT_TERMINAL_PROMPT=0 git pull --ff-only -q >"$LOG_DIR/update.log" 2>&1 &
      gp=$!
      for _ in $(seq 1 100); do kill -0 "$gp" 2>/dev/null || break; sleep 0.2; done
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
  if [ -z "$PY" ] || ! "$PY" -c "import importlib.util as u,sys; sys.exit(0 if all(u.find_spec(m) for m in ('streamlit','apscheduler')) else 1)" 2>/dev/null; then
    notify "Python environment not ready. In Terminal run: cd \"$REPO\" && python3 -m venv venv && venv/bin/pip install -r requirements.txt"
    return 1
  fi

  if [ "${ALPHAWAVE_CHECK:-0}" = "1" ]; then echo "Environment OK ($PY)"; return 0; fi

  # --- restart if the running code is older than the checked-out code -------------------
  version="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  restarted=""
  running=""; { alive "$RUN_DIR/scheduler.pid" || alive "$RUN_DIR/dashboard.pid"; } && running="yes"
  if [ -n "$running" ] && [ "$(cat "$RUN_DIR/version" 2>/dev/null)" = "$version" ]; then
    # Already running the latest code: ask what to do (ALPHAWAVE_ACTION=open|restart|stop skips the dialog).
    action="${ALPHAWAVE_ACTION:-}"
    if [ -z "$action" ] && command -v osascript >/dev/null 2>&1; then
      choice="$(osascript -e 'button returned of (display dialog "AlphaWave is running." & return & "Stopping only ends the local scheduler and dashboard; your positions and orders at the broker are not touched." buttons {"Stop", "Restart", "Open dashboard"} default button "Open dashboard" with title "AlphaWave")' 2>/dev/null)" \
        || return 0   # dialog cancelled: do nothing
      case "$choice" in Stop) action=stop;; Restart) action=restart;; *) action=open;; esac
    fi
    case "${action:-open}" in
      stop)    o1="$(term_proc scheduler)"; o2="$(term_proc dashboard)"
               [ -n "$o1" ] && wait_dead "$o1" 10; [ -n "$o2" ] && wait_dead "$o2" 5; notify "Stopped the scheduler and dashboard. Positions at the broker are unchanged."; return 0;;
      restart) running="restart";;
    esac
  fi
  sched_old=""; sched_bg=""
  if [ -n "$running" ] && { [ "$running" = "restart" ] || [ "$(cat "$RUN_DIR/version" 2>/dev/null)" != "$version" ]; }; then
    # Stop both at once. The dashboard comes back immediately; the scheduler restarts in the background
    # as soon as the old one has exited (it posts its Discord "stopped" alert first), so the browser isn't kept waiting.
    sched_old="$(term_proc scheduler)"; dash_old="$(term_proc dashboard)"
    [ -n "$dash_old" ] && wait_dead "$dash_old" 5
    for _ in $(seq 1 25); do port_open || break; sleep 0.2; done
    restarted="yes"
  fi
  echo "$version" >"$RUN_DIR/version"

  started=""
  if [ -n "$restarted" ]; then
    ( [ -n "$sched_old" ] && wait_dead "$sched_old" 10; start_scheduler ) &
    sched_bg=$!
  elif alive "$RUN_DIR/scheduler.pid"; then
    echo "Scheduler already running (pid $(cat "$RUN_DIR/scheduler.pid"))."
  else
    start_scheduler
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
  for _ in $(seq 1 150); do port_open && break; sleep 0.2; done
  if ! port_open; then notify "Dashboard did not start. See $LOG_DIR/dashboard.log"; return 1; fi
  if command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1; fi

  if [ -n "$restarted" ]; then notify "Updated${pulled:+ to the latest version} and restarted. Dashboard: $URL"
  elif [ -n "$started" ]; then notify "Started $started${pulled:+ (latest version)}. Dashboard: $URL"
  else notify "Already running the latest version. Opened $URL"; fi
  [ -n "$sched_bg" ] && wait "$sched_bg"   # the scheduler restart finishes in the background after the browser opened
  return 0
}

main "$@"
exit $?
