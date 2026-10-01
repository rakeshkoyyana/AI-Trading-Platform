from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from src.dashboard import metrics as m
from src.db.schema import SystemEvent, Trade, get_engine, init_db, session_scope

from src.config import PROJECT_ROOT

APP = PROJECT_ROOT / "src" / "dashboard" / "app.py"
NOW = datetime(2026, 10, 7, 20, 0)  # Wed 15:00 CT


def trade(pnl, days_ago=0, mode="paper", sent=None, direction="long", status="closed", sym="AAA"):
    t = NOW - timedelta(days=days_ago, hours=1)
    return dict(id=0, symbol=sym, entry_time=t - timedelta(hours=1), exit_time=t, direction=direction, entry_price=100.0,
                exit_price=100.0 + pnl, qty=1.0, pnl=pnl, signal_id=None, model_probability=None, sentiment_at_entry=sent,
                stop_loss=None, take_profit=None, mode=mode, status=status, note=None, signal_type="triple_confirmation", zone=None)


def df(*rows):
    return pd.DataFrame(list(rows))


def test_max_drawdown_and_sharpe():
    assert m.max_drawdown(pd.Series([100, 110, 99, 120])) == pytest.approx(-0.10)
    assert m.max_drawdown(pd.Series([], dtype=float)) == 0.0
    assert m.sharpe_ratio(pd.Series([0.01] * 5)) is None  # zero variance
    assert m.sharpe_ratio(pd.Series([0.01, -0.005, 0.02, 0.0])) > 0


def test_kpis_periods_and_ratios():
    t = df(trade(100, 0), trade(-50, 0), trade(200, 3), trade(-100, 40), trade(10, 0, status="open"))
    k = m.kpis(t, 100_000, NOW)
    assert k["trades"] == 4 and k["wins"] == 2 and k["losses"] == 2
    assert k["pnl_today"] == pytest.approx(50)
    assert k["pnl_week"] == pytest.approx(50)  # Wed: week starts Mon; 3 days ago is previous week
    assert k["pnl_month"] == pytest.approx(250)  # Oct 1+ -> includes the 3-days-ago trade
    assert k["pnl_all"] == pytest.approx(150)
    assert k["win_rate"] == 0.5
    assert k["win_loss_ratio"] == pytest.approx(150 / 75)
    assert k["profit_factor"] == pytest.approx(300 / 150)
    assert k["open_exposure"] == pytest.approx(100.0)  # only the open trade counts


def test_kpis_empty_is_safe():
    k = m.kpis(pd.DataFrame(columns=m.TRADE_COLS + ["signal_type", "zone"]), now=NOW)
    assert k["trades"] == 0 and k["win_rate"] is None and k["pnl_all"] == 0.0 and k["max_drawdown"] == 0.0


def test_equity_curve_keeps_mode_for_shading():
    t = df(trade(100, 2, "paper"), trade(-30, 1, "live"))
    eq = m.equity_curve(t, 1000)
    assert list(eq["equity"]) == [1100, 1070] and list(eq["mode"]) == ["paper", "live"]


def test_winrate_groups():
    t = df(trade(10, sent=0.5), trade(-5, sent=0.6), trade(7, sent=-0.5), trade(3, sent=0.0), trade(-1, direction="short", sent=None))
    s = m.winrate_by_sentiment(t).set_index("group")
    assert s.loc["positive (> 0.2)", "trades"] == 2 and s.loc["positive (> 0.2)", "win_rate"] == 0.5
    assert s.loc["negative (< -0.2)", "win_rate"] == 1.0
    assert m.winrate_by_sentiment(t)["trades"].sum() == 4  # no-sentiment trade excluded
    g = m.winrate_by_signal(t).set_index("group")
    assert g.loc["triple_confirmation / short", "win_rate"] == 0.0


def test_load_and_health_from_db(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path/'d.db'}")
    init_db(eng)
    with session_scope(eng) as s:
        s.add(Trade(symbol="AAA", direction="long", entry_price=10, exit_price=11, qty=2, pnl=2, status="closed",
                    entry_time=NOW - timedelta(hours=3), exit_time=NOW - timedelta(hours=2)))
        s.add(SystemEvent(timestamp=m.utcnow() - timedelta(minutes=10), kind="cycle", message="ok"))
        s.add(SystemEvent(timestamp=m.utcnow() - timedelta(minutes=5), kind="error", message="x"))
    t = m.load_trades(eng)
    assert len(t) == 1 and t["signal_type"].iat[0] == "manual/unknown"
    h = m.health(eng, m.utcnow() - timedelta(minutes=20))
    assert h["errors_24h"] == 1 and 9 < h["minutes_since_cycle"] < 12 and 19 < h["data_stale_minutes"] < 21
    assert m.load_events(eng, kinds=["error"]).shape[0] == 1


def test_app_renders_on_empty_and_populated_db(tmp_path, monkeypatch):
    """Smoke test: the Streamlit script runs end-to-end without exceptions."""
    from streamlit.testing.v1 import AppTest

    import src.config.settings as cfg

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'app.db'}")
    cfg.get_settings.cache_clear()
    import streamlit as st

    st.cache_data.clear()
    st.cache_resource.clear()
    try:
        at = AppTest.from_file(str(APP), default_timeout=120).run()
        assert not at.exception, [e.value for e in at.exception]  # empty DB
        eng = get_engine(f"sqlite:///{tmp_path/'app.db'}")
        with session_scope(eng) as s:
            s.add(Trade(symbol="SPY", direction="long", entry_price=10, exit_price=11, qty=2, pnl=2, status="closed",
                        entry_time=m.utcnow() - timedelta(hours=3), exit_time=m.utcnow() - timedelta(hours=2)))
        import streamlit as st

        st.cache_data.clear()
        at = AppTest.from_file(str(APP), default_timeout=120).run()
        assert not at.exception, [e.value for e in at.exception]
        assert any(x.label == "Closed trades" and x.value == "1" for x in at.metric)
    finally:
        cfg.get_settings.cache_clear()
