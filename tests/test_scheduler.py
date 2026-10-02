from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import select

from src.config.settings import Settings
from src.data_ingestion.synthetic import make_bars
from src.db.schema import SystemEvent, Trade, get_engine, init_db, session_scope
from src.execution.sim_broker import SimBroker
from src.scheduler import market_hours as mh
from src.scheduler.run_loop import TradingCycle, build_scheduler

S = Settings(max_stop_pct=0.5, min_stop_atr=0.0, tickers=["AAA", "BBB"], sip_delay_minutes=0, live_hybrid=False, exit_mode="bracket", default_trade_mode="auto")


def utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi)


# ------------------------------------------------------------- market hours
def test_weekday_session_bounds_are_830_to_1500_central():
    b = mh.session_bounds(date(2026, 10, 7), S)
    assert (b[0].hour, b[0].minute, b[1].hour, b[1].minute) == (8, 30, 15, 0)


def test_weekend_and_holiday_have_no_session():
    assert mh.session_bounds(date(2026, 10, 10), S) is None  # Saturday
    assert mh.session_bounds(date(2026, 11, 26), S) is None  # Thanksgiving
    assert not mh.is_trading_window_now(utc(2026, 11, 26, 16), S)


def test_early_close_shortens_session():
    b = mh.session_bounds(date(2026, 11, 27), S)  # day after Thanksgiving, 1pm ET close
    assert (b[1].hour, b[1].minute) == (12, 0)
    assert mh.flatten_time(date(2026, 11, 27), S).minute == 55
    assert mh.entry_cutoff(date(2026, 11, 27), S).strftime("%H:%M") == "11:45"


def test_window_edges_open_inclusive_close_exclusive():
    # 2026-10-07 is CDT (UTC-5): 08:30 CT = 13:30 UTC, 15:00 CT = 20:00 UTC
    assert not mh.is_trading_window_now(utc(2026, 10, 7, 13, 29), S)
    assert mh.is_trading_window_now(utc(2026, 10, 7, 13, 30), S)
    assert mh.is_trading_window_now(utc(2026, 10, 7, 19, 59), S)
    assert not mh.is_trading_window_now(utc(2026, 10, 7, 20, 0), S)


def test_entry_cutoff_blocks_new_positions_before_close():
    assert mh.can_open_new_positions(utc(2026, 10, 7, 19, 44), S)  # 14:44 CT
    assert not mh.can_open_new_positions(utc(2026, 10, 7, 19, 45), S)  # 14:45 CT
    assert not mh.can_open_new_positions(utc(2026, 10, 7, 13, 0), S)  # pre-open


def test_dst_shift_handled():
    # 2026-11-09 is CST (UTC-6): open = 14:30 UTC
    assert not mh.is_trading_window_now(utc(2026, 11, 9, 14, 29), S)
    assert mh.is_trading_window_now(utc(2026, 11, 9, 14, 30), S)


def test_next_session_open_skips_weekend():
    nxt = mh.next_session_open(utc(2026, 10, 9, 21, 0), S)  # Fri after close
    assert nxt.date() == date(2026, 10, 12) and (nxt.hour, nxt.minute) == (8, 30)


def test_bar_is_closed():
    t = utc(2026, 10, 7, 14, 0)
    assert not mh.bar_is_closed(t, 15, utc(2026, 10, 7, 14, 14))
    assert mh.bar_is_closed(t, 15, utc(2026, 10, 7, 14, 15))


# --------------------------------------------------------------- the cycle
@pytest.fixture()
def world(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path/'t.db'}")
    init_db(engine)
    frames = {"AAA": make_bars(n_days=60, seed=3), "BBB": make_bars(n_days=60, seed=11)}
    msgs = []
    broker = SimBroker()

    clock = {"now": None}

    def fetch(sym, tf, start, end):
        # simulated data feed: only bars that have started by the simulated "now" exist yet
        df = frames[sym]
        return df[df["timestamp"] <= clock["now"]].reset_index(drop=True)

    def make(settings=S, **kw):
        return TradingCycle(
            engine=engine, broker=broker, settings=settings, fetch=fetch,
            notify=lambda m, level="info", engine=None, post=True: msgs.append((level, m)),
            sentiment_fn=lambda s: dict(score=0.0, n=2), state_path=tmp_path / "state.json", **kw,
        )

    return dict(engine=engine, frames=frames, msgs=msgs, broker=broker, make=make, tmp=tmp_path, clock=clock)


def _now_after(world, df, k=None):
    """A 'now' just after the k-th bar closes (default last); moves the fake feed's clock too."""
    ts = df["timestamp"].iat[-1 if k is None else k]
    world["clock"]["now"] = ts
    return (ts + timedelta(minutes=15, seconds=30)).to_pydatetime()


def test_outside_window_is_skipped(world):
    c = world["make"]()
    assert "skipped" in c.run_cycle(now=utc(2026, 10, 10, 15))  # Saturday


def test_full_cycle_places_orders_logs_and_never_crashes(world):
    c = world["make"]()
    total = 0
    df = world["frames"]["AAA"]
    # replay many consecutive bars through the live path
    for k in range(len(df) - 400, len(df), 3):
        now = _now_after(world, df, k)
        for sym in S.tickers:
            world["broker"].set_price(sym, float(world["frames"][sym]["close"].iat[k]))
        out = c.run_cycle(now=now, force=True)
        assert "error" not in out, out
        assert not [s for s, r in out["symbols"].items() if r.get("error")], out["symbols"]
        total += out["trades"]
    assert total >= 1, "expected at least one paper trade over ~130 replayed cycles"
    with session_scope(world["engine"]) as s:
        trades = list(s.execute(select(Trade)).scalars())
        events = list(s.execute(select(SystemEvent)).scalars())
    assert len(trades) >= 1
    assert all(t.stop_loss and t.take_profit and t.mode == "paper" for t in trades)
    assert any(e.kind == "cycle" for e in events)


def test_forming_bar_is_dropped(world):
    c = world["make"]()
    df = world["frames"]["AAA"]
    world["clock"]["now"] = df["timestamp"].iat[-1]
    now = (df["timestamp"].iat[-1] + timedelta(minutes=5)).to_pydatetime()  # last bar still forming
    bars = c._closed_bars("AAA", now)
    assert bars["timestamp"].iat[-1] == df["timestamp"].iat[-2]


def test_symbol_error_is_isolated(world):
    df = world["frames"]
    good_fetch = None

    c = world["make"]()
    orig = c.fetch

    def flaky(sym, tf, start, end):
        if sym == "AAA":
            raise RuntimeError("boom")
        return orig(sym, tf, start, end)

    c.fetch = flaky
    out = c.run_cycle(now=_now_after(world, df["BBB"]), force=True)
    assert "error" not in out  # cycle survived
    assert out["symbols"]["BBB"].get("error") is None


def test_broker_failure_does_not_raise(world):
    c = world["make"]()

    def bad():
        raise ConnectionError("down")

    c.broker.get_account = bad
    out = c.run_cycle(now=_now_after(world, world["frames"]["AAA"]), force=True)
    assert "error" in out and any(l == "error" for l, _ in world["msgs"])


def test_too_few_bars_is_skipped_not_traded(world):
    c = world["make"]()
    now = _now_after(world, world["frames"]["AAA"], 100)
    out = c.run_cycle(now=now, force=True)
    assert all("skipped" in r for r in out["symbols"].values())
    assert out["trades"] == 0


def test_kill_switch_blocks_all_trades(world, tmp_path):
    ks = tmp_path / "KS"
    ks.write_text("x")
    s = Settings(max_stop_pct=0.5, min_stop_atr=0.0, tickers=["AAA", "BBB"], kill_switch_file=ks, sip_delay_minutes=0, live_hybrid=False, exit_mode="bracket", default_trade_mode="auto")
    c = world["make"](settings=s)
    df = world["frames"]["AAA"]
    for k in range(len(df) - 300, len(df), 3):
        out = c.run_cycle(now=_now_after(world, df, k), force=True)
        assert out["trades"] == 0


# ------------------------------------------------------------ session hooks
def test_start_session_stores_day_start_equity(world):
    c = world["make"]()
    now = utc(2026, 10, 7, 13, 25)
    c.start_session(now)
    assert c.day_start_equity(now) == pytest.approx(100_000.0)
    assert any("Session started" in m for _, m in world["msgs"])
    assert (world["tmp"] / "state.json").exists()


def test_start_session_ignores_weekend(world):
    c = world["make"]()
    c.start_session(utc(2026, 10, 10, 13, 25))
    assert not world["msgs"]


def test_flatten_runs_once_at_cutoff(world):
    c = world["make"]()
    world["broker"].set_price("AAA", 100.0)
    world["broker"].place_order("AAA", "buy", 10, 90.0, 120.0)
    assert not c.maybe_flatten(utc(2026, 10, 7, 19, 54))  # 14:54 CT, too early
    assert c.maybe_flatten(utc(2026, 10, 7, 19, 55))  # 14:55 CT
    assert not c.maybe_flatten(utc(2026, 10, 7, 19, 56))  # already done today
    assert world["broker"].get_positions() == []


def test_flatten_respects_setting_and_after_close(world):
    s = Settings(max_stop_pct=0.5, min_stop_atr=0.0, tickers=["AAA"], flatten_at_close=False, sip_delay_minutes=0, live_hybrid=False, exit_mode="bracket", default_trade_mode="auto")
    assert not world["make"](settings=s).maybe_flatten(utc(2026, 10, 7, 19, 56))
    assert not world["make"]().maybe_flatten(utc(2026, 10, 7, 20, 30))


def test_end_session_summarises(world):
    c = world["make"]()
    out = c.end_session(utc(2026, 10, 7, 20, 2))
    assert out["trades"] == 0 and out["equity"] == pytest.approx(100_000.0)
    assert any("Session ended" in m for _, m in world["msgs"])


def test_build_scheduler_registers_all_jobs(world):
    sched = build_scheduler(world["make"]())
    assert {j.id for j in sched.get_jobs()} == {"session_start", "cycle", "flatten", "session_end", "sentiment", "approvals", "closes", "reconcile"}


def test_model_that_does_not_improve_never_gates_trades():
    from src.scheduler.run_loop import gating_bundle

    bad = dict(version="v1", metrics=dict(improves=False))
    good = dict(version="v2", metrics=dict(improves=True))
    assert gating_bundle(None, S) == (None, "none")
    b, note = gating_bundle(bad, S)
    assert b is None and "did not beat" in note
    assert gating_bundle(good, S)[0] is good
    opt_in = Settings(use_unvalidated_model=True)
    assert gating_bundle(bad, opt_in)[0] is bad


def test_cycle_schedule_and_bar_horizon_follow_the_sip_delay():
    from src.scheduler.run_loop import _cron_minutes

    assert _cron_minutes(15) == "0,15,30,45"
    assert _cron_minutes(15, 16) == "16,31,46,1"  # fire 16 min after each bar close
    assert _cron_minutes(60, 16) == "16"


def test_latest_bar_is_withheld_until_it_is_inside_the_data_horizon(tmp_path):
    """With a 16-minute SIP delay, the bar that closed 5 minutes ago must not be used yet."""
    import dataclasses

    from src.data_ingestion.synthetic import make_bars

    bars = make_bars(n_days=3, seed=2)
    last_open = bars["timestamp"].iat[-1].to_pydatetime()
    now = last_open + timedelta(minutes=15 + 5)  # last bar closed 5 minutes ago

    def run(delay):
        s = dataclasses.replace(S, sip_delay_minutes=delay)
        cyc = TradingCycle(engine=get_engine(f"sqlite:///{tmp_path}/d{delay}.db"), broker=SimBroker(), settings=s,
                           fetch=lambda sym, tf, a, b: bars, notify=lambda *a, **k: None, state_path=tmp_path / f"s{delay}.json")
        return cyc._closed_bars("AAA", now)

    assert run(0)["timestamp"].iat[-1] == bars["timestamp"].iat[-1]
    assert run(16)["timestamp"].iat[-1] == bars["timestamp"].iat[-2]
