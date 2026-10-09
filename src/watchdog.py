"""Tiny independent process: alerts Discord if the scheduler goes quiet during market hours.

Runs separately from the scheduler (no torch / no model), so it still works when the scheduler was killed
(out of memory, kill -9) and could not announce it. Started and stopped by the AlphaWave app.
  python -m src.watchdog
"""
from __future__ import annotations

import signal
import sys
import time

from src import alerts, heartbeat
from src.config import get_settings
from src.scheduler.market_hours import utc_now

POLL_S = 20
REMIND_EVERY_S = 15 * 60


def step(state: dict, now_ts: float, now_utc=None, settings=None, notify=None, path=None) -> dict:
    """One check. `state` carries {'down_since', 'last_alert'} between calls. Returns the new state."""
    notify = notify or (lambda msg, level: alerts.notify(msg, level))
    verdict, msg = heartbeat.check(now_utc or utc_now(), settings, path=path, now_ts=now_ts,
                                   grace_since=state.get("started"))
    if verdict == "down":
        if not state.get("down_since"):
            notify(f"SCHEDULER DOWN: {msg}", "error")
            return {**state, "down_since": now_ts, "last_alert": now_ts}
        if now_ts - state.get("last_alert", 0) >= REMIND_EVERY_S:
            notify(f"STILL DOWN ({int((now_ts - state['down_since']) // 60)} min): {msg}", "error")
            return {**state, "last_alert": now_ts}
        return state
    if state.get("down_since") and verdict == "ok":
        notify(f"Scheduler is running again (was down {int((now_ts - state['down_since']) // 60)} min).", "info")
        return {k: v for k, v in state.items() if k not in {"down_since", "last_alert"}}
    if verdict == "idle":  # market closed: forget old alerts quietly
        return {k: v for k, v in state.items() if k not in {"down_since", "last_alert"}}
    return state


def main() -> None:
    get_settings()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    state = {"started": time.time()}
    print("[watchdog] watching the scheduler heartbeat during market hours", flush=True)
    while True:
        try:
            state = step(state, time.time())
        except Exception as exc:  # noqa: BLE001 - the watchdog itself must not die
            print(f"[watchdog] check failed: {exc}", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
