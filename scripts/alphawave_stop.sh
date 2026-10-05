#!/usr/bin/env bash
# Optional (terminal only): stop the local scheduler + dashboard. Never touches positions or orders at the broker.
REPO="${ALPHAWAVE_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
for name in scheduler dashboard; do
  pf="$REPO/data/run/$name.pid"
  [ -f "$pf" ] && { pid="$(cat "$pf")"; kill "$pid" 2>/dev/null; for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done; kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null; rm -f "$pf"; echo "Stopped $name."; }
done
exit 0
