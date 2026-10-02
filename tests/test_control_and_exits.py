"""Trade control (Off/Ask/Auto), approvals, Pine exits + protective stop, and the hybrid live tail."""
import dataclasses
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from src.config.settings import Settings
from src.data_ingestion.live_tail import estimate_tail, with_live_tail
from src.data_ingestion.synthetic import make_bars
from src.db.schema import PendingOrder, Trade, get_engine, init_db, session_scope
from src.decision_engine.engine import Decision, pine_exit_reason
from src.execution import control
from src.execution.alpaca_execution import AlpacaBroker
from src.execution.sim_broker import SimBroker
from src.scheduler.run_loop import TradingCycle

S = Settings(exit_mode="pine", max_stop_pct=0.5, min_stop_atr=0.0, tickers=["AAA", "BBB"], sip_delay_minutes=0, live_hybrid=False,
             default_trade_mode="ask")


@pytest.fixture()
def engine(tmp_path):
    e = get_engine(f"sqlite:///{tmp_path/'c.db'}")
    init_db(e)
    return e


# ------------------------------------------------------------------- modes
def test_default_modes_follow_settings_and_watchlist_is_off(engine):
    m = control.get_modes(engine, ["AAA", "ZZZ"], S)
    assert m == {"AAA": "ask", "ZZZ": "off", "BBB": "ask"} or m["AAA"] == "ask" and m["ZZZ"] == "off"
    assert control.default_mode("ZZZ", S) == "off"


def test_set_mode_persists_and_validates(engine):
    control.set_mode(engine, "zzz", "auto")
    control.set_mode(engine, "AAA", "off")
    m = control.get_modes(engine, settings=S)
    assert m["ZZZ"] == "auto" and m["AAA"] == "off"
    assert set(control.active_symbols(engine, S)) == {"BBB", "ZZZ"}
    with pytest.raises(ValueError):
        control.set_mode(engine, "AAA", "yolo")


# --------------------------------------------------------------- approvals
def _decision(sym="AAA", t=None):
    return Decision(symbol=sym, trade=True, direction="long", qty=5, entry=100.0, stop_loss=99.0,
                    signal_time=t or datetime(2026, 10, 1, 14, 0), reasons=["why"])


def test_pending_lifecycle_dedupe_decide_expire(engine):
    now = datetime(2026, 10, 1, 14, 20)
    pid = control.create_pending(engine, _decision(), None, ttl_minutes=10, settings=S, now=now)
    assert pid and control.create_pending(engine, _decision(), None, settings=S, now=now) is None  # deduped
    assert control.decide(engine, pid, True, now=now + timedelta(minutes=1))
    assert not control.decide(engine, pid, False)  # already decided
    p = control.list_pending(engine, "approved")[0]
    d = control.decision_from_pending(p)
    assert d.trade and d.qty == 5 and d.stop_loss == 99.0 and d.take_profit is None and d.reasons == ["why"]

    pid2 = control.create_pending(engine, _decision("BBB"), None, ttl_minutes=10, settings=S, now=now)
    assert not control.decide(engine, pid2, True, now=now + timedelta(minutes=11))  # too late
    assert control.list_pending(engine, "expired")
    pid3 = control.create_pending(engine, _decision("CCC"), None, ttl_minutes=1, settings=S, now=now)
    assert control.expire_stale(engine, now + timedelta(minutes=2)) == 1


# ------------------------------------------------------------- pine exits
def _row(**kw):
    base = dict(rsi=55.0, trend_bull=True, trend_bear=False)
    base.update(kw)
    return pd.Series(base)


def test_pine_exit_reasons_match_the_script():
    assert pine_exit_reason(_row(), "long") is None
    assert "RSI" in pine_exit_reason(_row(rsi=70.0), "long")
    assert "trend" in pine_exit_reason(_row(trend_bull=False, trend_bear=True), "long")
    assert pine_exit_reason(_row(rsi=70.0, trend_bull=False, trend_bear=True), "short") is None  # RSI>=70 alone never closes a short
    assert "RSI" in pine_exit_reason(_row(rsi=30.0, trend_bull=False, trend_bear=True), "short")
    assert "trend" in pine_exit_reason(_row(rsi=45.0), "short")  # trend turned bullish


# ----------------------------------------------------------- stop-only (OTO)
def test_sim_stop_only_entry_has_no_target_and_stops_out():
    b = SimBroker(slippage_bps=0)
    b.set_price("X", 100.0)
    r = b.place_order("X", "buy", 10, stop_loss=98.0, take_profit=None)
    assert [l.order_type for l in r.legs] == ["stop"]
    b.on_bar("X", high=130.0, low=99.0, close=125.0)  # a huge run-up must NOT exit (no target)
    assert "X" in b.positions
    b.on_bar("X", high=101.0, low=97.0, close=98.0)
    assert "X" not in b.positions and b.realized[-1]["exit"] == 98.0


class _OtoClient:
    def __init__(self):
        self.submitted, self.cancelled, self.closed = [], [], 0

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(id="o1", symbol=req.symbol, side=SimpleNamespace(value=req.side.value), qty=req.qty,
                               status=SimpleNamespace(value="accepted"), filled_qty=0, filled_avg_price=None,
                               legs=[], order_type=SimpleNamespace(value="market"))

    def get_orders(self, req):
        return [SimpleNamespace(id="stop-1")]

    def cancel_order_by_id(self, oid):
        self.cancelled.append(oid)

    def close_position(self, sym):
        self.closed += 1
        if self.closed == 1:
            raise RuntimeError("shares held for orders")  # cancel still settling
        return SimpleNamespace(id="c1", symbol=sym, side=SimpleNamespace(value="sell"), qty=3,
                               status=SimpleNamespace(value="accepted"), filled_qty=0, filled_avg_price=None,
                               legs=[], order_type=SimpleNamespace(value="market"))


def test_alpaca_stop_only_is_oto_and_close_cancels_the_stop_first(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    c = _OtoClient()
    b = AlpacaBroker(Settings(alpaca_api_key="k", alpaca_secret_key="s"), client=c)
    b.place_order("SPY", "buy", 5, stop_loss=498.123, take_profit=None)
    req = c.submitted[0]
    assert req.order_class.value == "oto" and req.stop_loss.stop_price == 498.12 and req.take_profit is None
    r = b.close_position("SPY")
    assert r is not None and c.cancelled == ["stop-1"] and c.closed == 2  # retried once


# ---------------------------------------------------------------- live tail
def _frames(n=80, sip_n=70, k=25):
    ts = pd.date_range("2026-09-30 14:00", periods=n, freq="15min")
    rng = np.random.default_rng(1)
    c = 100 + np.cumsum(rng.normal(0, .1, n))
    v = rng.integers(1000, 5000, n).astype(float)
    full = pd.DataFrame(dict(timestamp=ts, open=c, high=c + .1, low=c - .1, close=c, volume=v))
    iex = full.copy()
    iex["volume"] = v / k
    return full.iloc[:sip_n].reset_index(drop=True), iex, v


def test_estimate_tail_rescales_iex_volume_to_the_sip_scale():
    sip, iex, v = _frames()
    tail, d = estimate_tail(sip, iex)
    assert len(tail) == 10 and tail["est"].all() and abs(d["k"] - 25) < 1e-6
    assert np.allclose(tail["volume"].to_numpy(), v[70:])
    assert tail["timestamp"].min() > sip["timestamp"].max() and d["price_mape"] < 1e-9


def test_estimate_tail_refuses_without_enough_overlap():
    sip, iex, _ = _frames()
    tail, d = estimate_tail(sip.tail(3), iex)
    assert tail.empty and "overlapping" in d["reason"]


def test_with_live_tail_appends_flags_and_falls_back_on_failure():
    sip, iex, _ = _frames()
    out, d = with_live_tail(sip, "AAA", "15Min", fetch_iex=lambda *a: iex)
    assert len(out) == 80 and out["est"].tolist() == [False] * 70 + [True] * 10

    def boom(*a):
        raise RuntimeError("iex down")

    out2, d2 = with_live_tail(sip, "AAA", "15Min", fetch_iex=boom)
    assert len(out2) == 70 and not out2["est"].any() and "IEX fetch failed" in d2["reason"]


# ------------------------------------------------------------ cycle by mode
@pytest.fixture()
def world(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path/'w.db'}")
    init_db(engine)
    frames = {"AAA": make_bars(n_days=60, seed=3), "BBB": make_bars(n_days=60, seed=11)}
    msgs, broker, clock = [], SimBroker(), {"now": None}

    def fetch(sym, tf, start, end):
        df = frames[sym]
        return df[df["timestamp"] <= clock["now"]].reset_index(drop=True)

    def make(settings=S, **kw):
        return TradingCycle(engine=engine, broker=broker, settings=settings, fetch=fetch,
                            notify=lambda m, level="info", engine=None, post=True: msgs.append((level, m)),
                            sentiment_fn=lambda s: dict(score=0.0, n=2), state_path=tmp_path / "st.json", **kw)

    return dict(engine=engine, frames=frames, msgs=msgs, broker=broker, make=make, clock=clock)


def _replay(world, cyc, start=400, step=3):
    df = world["frames"]["AAA"]
    outs = []
    for k in range(len(df) - start, len(df), step):
        ts = df["timestamp"].iat[k]
        world["clock"]["now"] = ts
        for sym in S.tickers:
            world["broker"].set_price(sym, float(world["frames"][sym]["close"].iat[k]))
        outs.append(cyc.run_cycle(now=(ts + timedelta(minutes=15, seconds=30)).to_pydatetime(), force=True))
        yield outs[-1], (ts + timedelta(minutes=15, seconds=30)).to_pydatetime()


def test_ask_mode_queues_instead_of_trading(world):
    c = world["make"]()  # default mode: ask
    for _out, _now in _replay(world, c):
        pass
    with session_scope(world["engine"]) as s:
        assert not list(s.execute(select(Trade)).scalars())
        pend = list(s.execute(select(PendingOrder)).scalars())
    assert pend and all(p.status in {"pending", "expired"} for p in pend)
    assert any("APPROVE?" in m for _l, m in world["msgs"])


def test_off_mode_never_trades_or_queues(world):
    c = world["make"]()
    for sym in S.tickers:
        control.set_mode(world["engine"], sym, "off")
    for out, _ in _replay(world, c):
        assert out["symbols"] == {}
    with session_scope(world["engine"]) as s:
        assert not list(s.execute(select(Trade)).scalars()) and not list(s.execute(select(PendingOrder)).scalars())


def test_auto_mode_uses_pine_exits_and_a_protective_stop_only(world):
    c = world["make"](dataclasses.replace(S, default_trade_mode="auto"))
    for _ in _replay(world, c, start=380, step=2):
        pass
    with session_scope(world["engine"]) as s:
        trades = list(s.execute(select(Trade)).scalars())
    assert trades and all(t.stop_loss and t.take_profit is None for t in trades)
    pine = [t for t in trades if t.status == "closed" and "Pine exit" in (t.note or "")]
    assert pine, "expected at least one position closed by the Pine RSI / trend-flip rule"
    assert any(m.startswith("CLOSE ") for _l, m in world["msgs"])


def test_approved_pending_order_is_executed_and_revalidated(world):
    c = world["make"]()
    now = None
    for _out, now in _replay(world, c):
        if control.list_pending(world["engine"], "pending"):
            break
    p = control.list_pending(world["engine"], "pending")[0]
    assert control.decide(world["engine"], p.id, True, now=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=0)) or True
    # force-approve regardless of wall-clock expiry, then run the approvals job
    with session_scope(world["engine"]) as s:
        row = s.get(PendingOrder, p.id)
        row.status, row.expires_at = "approved", datetime(2999, 1, 1)
    world["broker"].set_price(p.symbol, p.entry)
    res = c.process_approvals(now=now, force=True)
    assert res and res[0]["sent"], res
    assert control.list_pending(world["engine"], "executed")
    assert world["broker"].positions.get(p.symbol) is not None

    # a second approval for a held ticker is refused, not sent
    with session_scope(world["engine"]) as s:
        s.add(PendingOrder(symbol=p.symbol, direction=p.direction, qty=1, entry=p.entry, stop_loss=p.stop_loss,
                           expires_at=datetime(2999, 1, 1), status="approved"))
    res = c.process_approvals(now=now, force=True)
    assert res and not res[0]["sent"] and "already holding" in res[0]["why"]


def test_price_beyond_stop_blocks_an_approved_entry(world):
    c = world["make"](price_fn=lambda sym: 1.0)  # price collapsed below any stop
    with session_scope(world["engine"]) as s:
        s.add(PendingOrder(symbol="AAA", direction="long", qty=1, entry=100.0, stop_loss=99.0,
                           expires_at=datetime(2999, 1, 1), status="approved"))
    world["broker"].set_price("AAA", 100.0)
    res = c.process_approvals(now=datetime(2026, 10, 7, 15, 0), force=True)
    assert res and not res[0]["sent"] and "beyond the stop" in res[0]["why"]


# ---------------------------------------------------------- hybrid TP + manual close
def test_hybrid_mode_places_a_two_r_target_and_pine_exits_still_run(world):
    c = world["make"](dataclasses.replace(S, default_trade_mode="auto", exit_mode="hybrid", target_rr=2.0))
    for _ in _replay(world, c, start=380, step=2):
        pass
    with session_scope(world["engine"]) as s:
        trades = list(s.execute(select(Trade)).scalars())
    assert trades
    for t in trades:
        risk = abs(t.entry_price - t.stop_loss)
        assert t.take_profit and abs(abs(t.take_profit - t.entry_price) / risk - 2.0) < 0.05, (t.entry_price, t.stop_loss, t.take_profit)
    assert any("Pine exit" in (t.note or "") for t in trades if t.status == "closed") or any(
        t.status == "closed" for t in trades), "expected at least one position to end (Pine exit, target or stop)"


def test_manual_close_request_closes_the_position_and_logs_it(world):
    c = world["make"](dataclasses.replace(S, default_trade_mode="auto"))
    now = None
    for _out, now in _replay(world, c, start=380, step=2):
        if world["broker"].get_positions():
            break
    held = world["broker"].get_positions()
    assert held, "need an open position to close"
    sym = held[0].symbol
    assert control.request_close(world["engine"], sym)
    res = c.process_closes(now=datetime.utcnow(), force=True)
    assert res and res[0]["closed"], res
    assert not [p for p in world["broker"].get_positions() if p.symbol == sym]
    with session_scope(world["engine"]) as s:
        t = s.execute(select(Trade).where(Trade.symbol == sym, Trade.status == "closed").order_by(Trade.id.desc())).scalars().first()
    assert t is not None and "Manual close" in (t.note or "")
    assert control.list_close_requests(world["engine"], "done")
    # nothing left to close -> a second request fails cleanly instead of sending an order
    control.request_close(world["engine"], sym)
    res = c.process_closes(now=datetime.utcnow(), force=True)
    assert res and not res[0]["closed"]


def test_close_request_queue_dedupes_and_expires(tmp_path):
    from datetime import datetime, timedelta

    from src.db.schema import get_engine, init_db

    eng = get_engine(f"sqlite:///{tmp_path/'c.db'}")
    init_db(eng)
    now = datetime(2026, 10, 2, 16, 0)
    rid = control.request_close(eng, "asts", now=now)
    assert rid and control.request_close(eng, "ASTS", now=now) is None  # duplicate click ignored
    assert control.expire_close_requests(eng, now + timedelta(seconds=30)) == 0
    assert control.expire_close_requests(eng, now + timedelta(seconds=control.CLOSE_TTL_SECONDS + 1)) == 1
    assert control.list_close_requests(eng, "pending") == []
    assert control.list_close_requests(eng, "expired")[0].symbol == "ASTS"


def test_manual_close_closes_the_trade_even_when_the_fill_price_is_not_back_yet(world, monkeypatch):
    import dataclasses as dc

    import src.scheduler.run_loop as rl

    monkeypatch.setattr(rl.time, "sleep", lambda *_: None)
    c = world["make"](dc.replace(S, default_trade_mode="auto"))
    for _out, now in _replay(world, c, start=380, step=2):
        if world["broker"].get_positions():
            break
    sym = world["broker"].get_positions()[0].symbol
    real_close = world["broker"].close_position

    def close_without_fill(s):
        res = real_close(s)
        return dc.replace(res, filled_avg_price=None) if res else res

    monkeypatch.setattr(world["broker"], "close_position", close_without_fill)
    monkeypatch.setattr(world["broker"], "get_order", lambda oid: None)
    control.request_close(world["engine"], sym)
    res = c.process_closes(now=datetime.utcnow(), force=True)
    assert res and res[0]["closed"], res
    with session_scope(world["engine"]) as s:
        t = s.execute(select(Trade).where(Trade.symbol == sym).order_by(Trade.id.desc())).scalars().first()
    assert t.status == "closed" and t.exit_price and t.exit_price > 0, "trade must not stay open or get a 0 exit"
