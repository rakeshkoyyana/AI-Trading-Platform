#!/usr/bin/env bash
# Stop the scheduler and dashboard started by alphawave_launcher.sh.
# Open positions and bracket orders stay live at the broker; only the local processes stop.
set -u

REPO="${ALPHAWAVE_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RUN_DIR="$REPO/data/run"

notify() {
  command -v osascript >/dev/null 2>&1 &&
    osascript -e "display notification \"$1\" with title \"AlphaWave\"" >/dev/null 2>&1
  echo "$1"
}

stopped=""
for name in scheduler dashboard; do
  pf="$RUN_DIR/$name.pid"
  if [ -f "$pf" ]; then
    pid="$(cat "$pf" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null
      for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
      kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
      stopped="${stopped:+$stopped + }$name"
    fi
    rm -f "$pf"
  fi
done

if [ -n "$stopped" ]; then
  notify "Stopped $stopped. Open positions keep their stop and target at the broker."
else
  notify "Nothing was running."
fi
