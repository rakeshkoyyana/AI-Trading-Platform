import math
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.config.settings import Settings
from src.data_ingestion.synthetic import make_bars
from src.db.schema import Trade, get_engine, init_db, session_scope
from src.decision_engine.engine import AccountState, should_trade
from src.execution.alpaca_execution import AlpacaBroker, get_broker
from src.execution.sim_broker import SimBroker
from src.execution.trade_log import close_trade, flatten_all, reconcile, record_order
from src.smc_logic import compute_context
from sqlalchemy import select


# ----------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def ctx_long():
    """Context truncated so its last closed bar carries a fresh LONG signal."""
    df = make_bars(n_days=120, seed=3)
    ctx = compute_context(df)
    idx = [i for i in np.where(ctx["signal_event"] == "long")[0] if i > 400 and not math.isnan(ctx["atr"].iat[i])]
    return ctx.iloc[: idx[0] + 1].reset_index(drop=True)


@pytest.fixture(scope="module")
def ctx_short():
    df = make_bars(n_days=120, seed=3)
    ctx = compute_context(df)
    idx = [i for i in np.where(ctx["signal_event"] == "short")[0] if i > 400 and not math.isnan(ctx["atr"].iat[i])]
    return ctx.iloc[: idx[0] + 1].reset_index(drop=True)


def acct(**kw):
    base = dict(equity=100_000.0, buying_power=200_000.0, day_start_equity=100_000.0, open_positions={})
    base.update(kw)
    return AccountState(**base)


S = Settings(max_stop_pct=0.5, min_stop_atr=0.0)  # permissive stop sanity so gates are tested one at a time


class FakeBundle(dict):
    pass


def bundle_with(p):
    class M:
        def predict_proba(self, X):
            return np.array([[1 - p, p]])

    from src.ml.features import FEATURES

    return dict(model=M(), features=FEATURES, version="fake_v1")


# ------------------------------------------------------------ decision engine
def test_no_signal_means_no_trade(ctx_long):
    ctx = ctx_long.copy()
    ctx.loc[ctx.index[-1], "signal_event"] = "none"
    d = should_trade("TEST", ctx, acct(), S)
    assert not d.trade and d.blocked_by is None and "no fresh" in d.reasons[0]


def test_happy_path_long_is_explainable_and_sized(ctx_long):
    d = should_trade("TEST", ctx_long, acct(), S, bundle=bundle_with(0.7))
    assert d.trade and d.direction == "long" and d.qty >= 1
    assert d.stop_loss < d.entry < d.take_profit
    text = d.explain()
    for needle in ("signal long", "model fake_v1", "stop", "size"):
        assert needle in text
    # position never exceeds the cap or the per-trade risk budget
    assert d.qty * d.entry <= 100_000 * S.max_position_pct + d.entry
    assert d.qty * abs(d.entry - d.stop_loss) <= 100_000 * S.risk_per_trade_pct + 1e-6


def test_short_signal_has_inverted_levels(ctx_short):
    d = should_trade("TEST", ctx_short, acct(), S, bundle=bundle_with(0.7))
    assert d.trade and d.direction == "short" and d.take_profit < d.entry < d.stop_loss


def test_model_gate_blocks_low_probability(ctx_long):
    d = should_trade("TEST", ctx_long, acct(), S, bundle=bundle_with(0.40))
    assert not d.trade and d.blocked_by == "model" and d.probability == pytest.approx(0.40)


def test_require_model_blocks_when_missing(ctx_long):
    s = Settings(require_model=True, max_stop_pct=0.5, min_stop_atr=0.0)
    assert should_trade("TEST", ctx_long, acct(), s, bundle=None).blocked_by == "no_model"
    assert should_trade("TEST", ctx_long, acct(), S, bundle=None).trade  # rule-only mode allowed


def test_sentiment_gate_blocks_opposing_news_only(ctx_long, ctx_short):
    bad = dict(score=-0.8, n=4)
    assert should_trade("T", ctx_long, acct(), S, sentiment=bad).blocked_by == "sentiment"
    assert should_trade("T", ctx_long, acct(), S, sentiment=dict(score=0.8, n=4)).trade
    assert should_trade("T", ctx_short, acct(), S, sentiment=dict(score=0.8, n=4)).blocked_by == "sentiment"
    assert should_trade("T", ctx_long, acct(), S, sentiment=dict(score=-0.8, n=0)).trade  # no items -> ignored


def test_daily_loss_halt(ctx_long):
    a = acct(equity=97_900.0)  # -2.1% on the day vs 2% limit
    d = should_trade("TEST", ctx_long, a, S, bundle=bundle_with(0.9))
    assert d.blocked_by == "daily_loss_halt"
    assert should_trade("TEST", ctx_long, acct(equity=98_500.0), S).trade  # -1.5% still fine


def test_max_positions_and_duplicate_symbol(ctx_long):
    full = acct(open_positions={"A": 10, "B": -5, "C": 7})
    assert should_trade("TEST", ctx_long, full, S).blocked_by == "max_positions"
    dup = acct(open_positions={"TEST": 10})
    assert should_trade("TEST", ctx_long, dup, S).blocked_by == "already_in_position"


def test_shorts_can_be_disabled(ctx_short):
    s = Settings(allow_shorts=False, max_stop_pct=0.5, min_stop_atr=0.0)
    assert should_trade("T", ctx_short, acct(), s).blocked_by == "shorts_disabled"


def test_stale_signal_is_blocked(ctx_long):
    bar_ts = pd.Timestamp(ctx_long["timestamp"].iat[-1]).to_pydatetime()
    fresh = bar_ts + timedelta(minutes=15 + 5)
    stale = bar_ts + timedelta(minutes=15 * 6)
    assert should_trade("T", ctx_long, acct(), S, now=fresh).trade
    assert should_trade("T", ctx_long, acct(), S, now=stale).blocked_by == "stale_signal"


def test_stop_sanity_rules(ctx_long):
    tight = Settings(max_stop_pct=0.0001, min_stop_atr=0.0)
    assert should_trade("T", ctx_long, acct(), tight).blocked_by == "stop_too_wide"
    noisy = Settings(max_stop_pct=0.5, min_stop_atr=1000.0)
    assert should_trade("T", ctx_long, acct(), noisy).blocked_by == "stop_too_tight"


def test_size_too_small_when_account_tiny(ctx_long):
    d = should_trade("T", ctx_long, acct(equity=100.0, day_start_equity=100.0, buying_power=100.0), S)
    assert d.blocked_by == "size_too_small"


def test_kill_switch_blocks_everything(ctx_long, tmp_path):
    ks = tmp_path / "KILL"
    ks.write_text("x")
    s = Settings(kill_switch_file=ks, max_stop_pct=0.5, min_stop_atr=0.0)
    assert should_trade("T", ctx_long, acct(), s).blocked_by == "kill_switch"
    ks.unlink()
    assert should_trade("T", ctx_long, acct(), s).trade


# ------------------------------------------------------------------ sim broker
def test_sim_bracket_target_and_stop_and_pnl():
    b = SimBroker(equity=100_000, slippage_bps=0)
    b.set_price("X", 100.0)
    r = b.place_order("X", "buy", 10, stop_loss=98.0, take_profit=104.0)
    assert r.status == "filled" and len(r.legs) == 2 and b.get_positions()[0].qty == 10
    b.on_bar("X", 103.0, 99.0, 101.0)  # nothing hit
    assert b.get_positions()
    b.on_bar("X", 105.0, 100.5, 104.5)  # target
    assert not b.get_positions() and b.realized[-1]["pnl"] == pytest.approx(40.0)
    assert b.get_order(r.id).legs[1].status == "filled" and b.get_order(r.id).legs[0].status == "canceled"

    b.set_price("X", 100.0)
    r2 = b.place_order("X", "sell", 10, stop_loss=102.0, take_profit=96.0)  # short
    b.on_bar("X", 102.5, 99.0, 101.0)  # stop wins
    assert b.realized[-1]["pnl"] == pytest.approx(-20.0)


def test_sim_stop_wins_when_both_touch():
    b = SimBroker(slippage_bps=0)
    b.set_price("X", 100.0)
    b.place_order("X", "buy", 5, stop_loss=98.0, take_profit=102.0)
    b.on_bar("X", 103.0, 97.0, 100.0)
    assert b.realized[-1]["exit"] == 98.0


def test_sim_rejects_when_killed(tmp_path, monkeypatch):
    import src.execution.sim_broker as sb

    ks = tmp_path / "K"
    ks.write_text("1")
    monkeypatch.setattr(sb, "get_settings", lambda: Settings(kill_switch_file=ks))
    b = SimBroker()
    b.set_price("X", 10.0)
    assert b.place_order("X", "buy", 1).status == "rejected"


# ----------------------------------------------------------- alpaca adapter
class FakeAlpacaClient:
    def __init__(self):
        self.submitted = []

    def submit_order(self, req):
        self.submitted.append(req)
        return SimpleNamespace(
            id="abc-123", symbol=req.symbol, side=SimpleNamespace(value=req.side.value), qty=req.qty,
            status=SimpleNamespace(value="accepted"), filled_qty=0, filled_avg_price=None, legs=[],
            order_type=SimpleNamespace(value="market"),
        )

    def get_account(self):
        return SimpleNamespace(equity="101000.5", cash="50000", buying_power="200000", last_equity="100000")

    def get_all_positions(self):
        return [SimpleNamespace(symbol="SPY", qty="-3", avg_entry_price="500.1", current_price="499", unrealized_pl="3.3")]


def test_alpaca_bracket_order_built_correctly():
    c = FakeAlpacaClient()
    b = AlpacaBroker(Settings(alpaca_api_key="k", alpaca_secret_key="s"), client=c)
    r = b.place_order("SPY", "buy", 5, stop_loss=498.123, take_profit=506.987)
    assert r.id == "abc-123" and r.status == "accepted"
    req = c.submitted[0]
    assert req.qty == 5 and req.stop_loss.stop_price == 498.12 and req.take_profit.limit_price == 506.99
    assert req.order_class.value == "bracket"
    assert b.is_paper
    acc = b.get_account()
    assert acc.equity == 101000.5 and acc.last_equity == 100000.0
    assert b.get_positions()[0].qty == -3 and b.get_positions()[0].direction == "short"


def test_alpaca_rejects_zero_qty_and_kill_switch(tmp_path):
    c = FakeAlpacaClient()
    b = AlpacaBroker(Settings(), client=c)
    assert b.place_order("SPY", "buy", 0).status == "rejected"
    ks = tmp_path / "K"
    ks.write_text("1")
    b2 = AlpacaBroker(Settings(kill_switch_file=ks), client=c)
    assert b2.place_order("SPY", "buy", 1).message == "kill switch active"
    assert c.submitted == []


def test_live_mode_requires_explicit_confirmation(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_CONFIRMED", raising=False)
    with pytest.raises(RuntimeError, match="LIVE_TRADING_CONFIRMED"):
        AlpacaBroker(Settings(trading_mode="live", alpaca_api_key="k", alpaca_secret_key="s"), client=FakeAlpacaClient())
    monkeypatch.setenv("LIVE_TRADING_CONFIRMED", "true")
    assert not AlpacaBroker(Settings(trading_mode="live"), client=FakeAlpacaClient()).is_paper


def test_get_broker_sim_switch(monkeypatch):
    monkeypatch.setenv("BROKER", "sim")
    assert isinstance(get_broker(Settings()), SimBroker)


# -------------------------------------------------------- logging/reconcile
@pytest.fixture()
def engine():
    e = get_engine("sqlite:///:memory:")
    init_db(e)
    return e


def _entry(engine, broker, ctx, symbol="X"):
    d = should_trade(symbol, ctx, acct(), S)
    broker.set_price(symbol, d.entry)
    order = broker.place_order(symbol, "buy" if d.direction == "long" else "sell", d.qty, d.stop_loss, d.take_profit)
    tid = record_order(engine, d, order, "paper")
    return d, order, tid


def test_order_logged_then_target_leg_closes_trade_with_pnl(engine, ctx_long):
    b = SimBroker(slippage_bps=0)
    d, order, tid = _entry(engine, b, ctx_long)
    with session_scope(engine) as s:
        t = s.get(Trade, tid)
        assert t.status == "filled" and t.qty == d.qty and t.stop_loss == d.stop_loss and t.mode == "paper"
    assert reconcile(engine, b) == []  # books match while the position is open
    b.on_bar("X", d.take_profit + 1, d.entry, d.take_profit)
    assert reconcile(engine, b) == []
    with session_scope(engine) as s:
        t = s.get(Trade, tid)
        assert t.status == "closed" and t.exit_price == pytest.approx(d.take_profit)
        assert t.pnl == pytest.approx((d.take_profit - d.entry) * d.qty) and "target" in t.note


def test_stop_leg_close_is_labelled(engine, ctx_long):
    b = SimBroker(slippage_bps=0)
    d, order, tid = _entry(engine, b, ctx_long)
    b.on_bar("X", d.entry, d.stop_loss - 0.5, d.stop_loss)
    reconcile(engine, b)
    with session_scope(engine) as s:
        t = s.get(Trade, tid)
        assert t.pnl < 0 and "stop" in t.note


def test_reconcile_flags_orphans_and_external_closes(engine, ctx_long):
    b = SimBroker(slippage_bps=0)
    b.set_price("ORPHAN", 10.0)
    b.place_order("ORPHAN", "buy", 5)  # position the DB knows nothing about
    alerts = []
    issues = reconcile(engine, b, lambda m, lvl: alerts.append((m, lvl)))
    assert any("orphan" in i for i in issues) and alerts and alerts[0][1] == "warning"

    d, order, tid = _entry(engine, b, ctx_long, symbol="X")
    b.positions.pop("X")  # someone closed it manually; no leg filled
    b.brackets.clear()
    issues = reconcile(engine, b)
    assert any("closed manually" in i for i in issues)
    with session_scope(engine) as s:
        assert s.get(Trade, tid).status == "closed"


def test_rejected_order_is_logged_as_rejected(engine, ctx_long):
    b = SimBroker()
    d = should_trade("NOPRICE", ctx_long, acct(), S)
    order = b.place_order("NOPRICE", "buy", d.qty)  # no price set -> rejected
    tid = record_order(engine, d, order, "paper")
    with session_scope(engine) as s:
        t = s.get(Trade, tid)
        assert t.status == "rejected" and t.entry_time is None


def test_flatten_all_closes_positions_and_trades(engine, ctx_long):
    b = SimBroker(slippage_bps=0)
    d, order, tid = _entry(engine, b, ctx_long)
    b.set_price("X", d.entry + 0.5)
    n = flatten_all(engine, b)
    assert n == 1 and not b.get_positions()
    with session_scope(engine) as s:
        t = s.get(Trade, tid)
        assert t.status == "closed" and t.pnl == pytest.approx(0.5 * d.qty) and "flattened" in t.note
