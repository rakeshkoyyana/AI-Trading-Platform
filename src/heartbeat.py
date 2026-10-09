"""Scheduler heartbeat + liveness check, shared by the watchdog process and the dashboard.

The scheduler writes data/run/heartbeat.json every few seconds. If the process is killed (out of memory, `kill -9`,
power loss) it can't say so itself, so something else has to notice the heartbeat going stale during market hours.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from src.config import Settings, get_settings
from src.data_ingestion.common import TIMEFRAME_MINUTES
from src.scheduler.market_hours import is_trading_window_now, session_bounds, to_local, utc_now

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HEARTBEAT_PATH = PROJECT_ROOT / "data" / "run" / "heartbeat.json"
STALE_AFTER_S = 120  # long enough for a restart (model load); jobs beat every ~15 s


def write(pid: int | None = None, last_cycle: float | None = None, started: float | None = None,
          path: Path | None = None) -> None:
    """Never raises: a heartbeat problem must not hurt trading."""
    p = path or HEARTBEAT_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        old = read(p) or {}
        data = dict(ts=time.time(), pid=pid or os.getpid(),
                    started=started if started is not None else old.get("started", time.time()),
                    last_cycle=last_cycle if last_cycle is not None else old.get("last_cycle"))
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(p)
    except Exception:  # noqa: BLE001
        pass


def read(path: Path | None = None) -> dict | None:
    try:
        return json.loads((path or HEARTBEAT_PATH).read_text())
    except Exception:  # noqa: BLE001
        return None


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:  # noqa: BLE001 - e.g. PermissionError: the process exists
        return True


def check(now_utc: datetime | None = None, settings: Settings | None = None, path: Path | None = None,
          now_ts: float | None = None, grace_since: float | None = None) -> tuple[str, str]:
    """Return (state, message). state: 'ok' | 'down' | 'idle' (outside trading hours, nothing to check).

    `grace_since`: wall-clock time the checker started; no 'down' verdict until STALE_AFTER_S after it
    (the scheduler may be mid-restart).
    """
    s = settings or get_settings()
    now_utc = now_utc or utc_now()
    ts = now_ts if now_ts is not None else time.time()
    if not is_trading_window_now(now_utc, s):
        return "idle", ""
    hb = read(path)
    if grace_since is not None and ts - grace_since < STALE_AFTER_S and (hb is None or ts - hb.get("ts", 0) > STALE_AFTER_S):
        return "ok", "starting up"
    if hb is None:
        return "down", "The scheduler has not started today (no heartbeat). Nothing is scanning for trades."
    age = ts - float(hb.get("ts", 0))
    pid = hb.get("pid")
    if age > STALE_AFTER_S:
        if pid and not _pid_alive(pid):
            why = f"its process (pid {pid}) is gone - it was killed or crashed without a stop alert"
        else:
            why = f"process {pid} is still there but frozen"
        return "down", (f"The scheduler stopped {int(age // 60)} min ago: {why}. Nothing is scanning for trades "
                        f"and Auto orders will not be sent. Open AlphaWave and choose Restart.")
    # alive: has a cycle finished recently? (a hung cycle keeps the heartbeat going but trades nothing)
    local = to_local(now_utc, s)
    b = session_bounds(local.date(), s)
    limit = (2 * TIMEFRAME_MINUTES.get(s.timeframe, 15) + 5) * 60
    if b and local >= b[0]:
        # measured from the later of: last finished cycle, scheduler start, today's session open
        ref = max(float(hb.get("last_cycle") or 0), float(hb.get("started") or 0), b[0].timestamp())
        stuck = ts - ref > limit
    else:
        stuck, ref = False, ts
    if stuck:
        return "down", (f"The scheduler is running but no trading cycle has finished for {int((ts - ref) // 60)} min "
                        f"- it looks stuck. Open AlphaWave and choose Restart.")
    return "ok", ""
