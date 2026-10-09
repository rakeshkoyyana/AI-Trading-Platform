"""Scheduler heartbeat + watchdog: silent death during market hours must be noticed."""
import dataclasses
import json
import os
from datetime import datetime

from src import heartbeat, watchdog
from src.config.settings import Settings

S = Settings()
# Wed 2026-10-07: session 08:30-15:00 CT = 13:30-20:00 UTC
OPEN = datetime(2026, 10, 7, 14, 0)    # 09:00 CT
CLOSED = datetime(2026, 10, 7, 22, 0)  # after close
T0 = 1_000_000.0


def _hb(tmp_path, ts, pid=None, started=None, last_cycle=None):
    p = tmp_path / "hb.json"
    p.write_text(json.dumps(dict(ts=ts, pid=pid or os.getpid(), started=started or ts - 3600, last_cycle=last_cycle)))
    return p


def _bounds_ts():
    from src.scheduler.market_hours import session_bounds
    return session_bounds(OPEN.date(), S)[0].timestamp()


def test_fresh_heartbeat_is_ok(tmp_path):
    t = _bounds_ts() + 1800
    p = _hb(tmp_path, t - 10, last_cycle=t - 60)
    assert heartbeat.check(OPEN, S, p, now_ts=t)[0] == "ok"


def test_stale_heartbeat_with_dead_pid_says_it_was_killed(tmp_path):
    t = _bounds_ts() + 1800
    p = _hb(tmp_path, t - 600, pid=2_999_999)
    state, msg = heartbeat.check(OPEN, S, p, now_ts=t)
    assert state == "down" and "gone" in msg and "killed" in msg and "Restart" in msg


def test_stale_heartbeat_with_live_pid_says_frozen(tmp_path):
    t = _bounds_ts() + 1800
    state, msg = heartbeat.check(OPEN, S, _hb(tmp_path, t - 600), now_ts=t)
    assert state == "down" and "frozen" in msg


def test_missing_heartbeat_in_market_hours_is_down(tmp_path):
    assert heartbeat.check(OPEN, S, tmp_path / "none.json", now_ts=_bounds_ts() + 600)[0] == "down"


def test_nothing_to_check_when_market_closed(tmp_path):
    assert heartbeat.check(CLOSED, S, tmp_path / "none.json", now_ts=T0)[0] == "idle"


def test_beating_but_no_cycle_for_35_minutes_is_stuck(tmp_path):
    t = _bounds_ts() + 40 * 60
    p = _hb(tmp_path, t - 5, started=t - 7200, last_cycle=None)
    state, msg = heartbeat.check(OPEN, S, p, now_ts=t)
    assert state == "down" and "stuck" in msg
    assert heartbeat.check(OPEN, S, _hb(tmp_path, t - 5, last_cycle=t - 300), now_ts=t)[0] == "ok"


def test_overnight_scheduler_is_not_flagged_before_the_first_cycle(tmp_path):
    t = _bounds_ts() + 120  # 2 min after the open; last cycle was yesterday
    p = _hb(tmp_path, t - 5, started=t - 86400, last_cycle=t - 86000)
    assert heartbeat.check(OPEN, S, p, now_ts=t)[0] == "ok"


def test_grace_period_after_watchdog_start(tmp_path):
    t = _bounds_ts() + 600
    state, _ = heartbeat.check(OPEN, S, tmp_path / "none.json", now_ts=t, grace_since=t - 30)
    assert state == "ok"


def test_watchdog_alerts_once_reminds_and_reports_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "HEARTBEAT_PATH", tmp_path / "hb.json")
    sent = []
    notify = lambda m, lvl: sent.append((lvl, m))  # noqa: E731
    t = _bounds_ts() + 1800
    p = _hb(tmp_path, t - 600, pid=2_999_999)
    st = {}
    st = watchdog.step(st, t, OPEN, S, notify, p)
    assert len(sent) == 1 and sent[0][0] == "error" and "SCHEDULER DOWN" in sent[0][1]
    st = watchdog.step(st, t + 60, OPEN, S, notify, p)          # no spam
    assert len(sent) == 1
    st = watchdog.step(st, t + 16 * 60, OPEN, S, notify, p)     # reminder after 15 min
    assert len(sent) == 2 and "STILL DOWN" in sent[1][1]
    p = _hb(tmp_path, t + 16 * 60 + 5, last_cycle=t + 16 * 60)  # scheduler restarted
    st = watchdog.step(st, t + 16 * 60 + 10, OPEN, S, notify, p)
    assert len(sent) == 3 and "running again" in sent[2][1] and "down_since" not in st


def test_watchdog_stays_quiet_outside_market_hours(tmp_path):
    sent = []
    watchdog.step({}, T0, CLOSED, S, lambda m, lvl: sent.append(m), tmp_path / "none.json")
    assert sent == []


def test_write_roundtrip_keeps_started(tmp_path):
    p = tmp_path / "hb.json"
    heartbeat.write(pid=1, started=5.0, path=p)
    heartbeat.write(pid=1, last_cycle=9.0, path=p)
    d = heartbeat.read(p)
    assert d["started"] == 5.0 and d["last_cycle"] == 9.0


def test_failed_discord_post_never_prints_the_webhook_url(monkeypatch, capsys):
    from src import alerts

    url = "https://discord.com/api/webhooks/123/SECRET-TOKEN"
    monkeypatch.setattr(alerts, "get_settings", lambda: dataclasses.replace(S, discord_webhook_url=url))
    monkeypatch.setattr(alerts, "log_event", lambda *a, **k: None)

    def boom(u, **kw):
        raise RuntimeError(f"Max retries exceeded with url: {u}")

    assert alerts.notify("x", "info", post=boom) is False
    out = capsys.readouterr().out
    assert "SECRET-TOKEN" not in out and "<webhook>" in out
