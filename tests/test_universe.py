"""Symbol search, research watchlist and on-demand data loading."""
import json
from pathlib import Path

import pytest

from src import universe as U
from src.data_ingestion.backfill import latest_bar_time, load_bars
from src.data_ingestion.on_demand import ensure_symbol_data
from src.data_ingestion.synthetic import make_bars
from src.db.schema import get_engine, init_db

ASSETS = [dict(symbol=s, name=n, exchange="NASDAQ") for s, n in [
    ("AAPL", "Apple Inc."), ("AAP", "Advance Auto Parts, Inc."), ("PLTR", "Palantir Technologies Inc."),
    ("SPY", "SPDR S&P 500 ETF Trust"), ("APLE", "Apple Hospitality REIT, Inc."), ("A", "Agilent Technologies, Inc.")]]


def test_search_ranks_exact_then_prefix_then_names():
    assert U.search_assets(ASSETS, "aapl")[0]["symbol"] == "AAPL"
    assert [a["symbol"] for a in U.search_assets(ASSETS, "aap")][:2] == ["AAP", "AAPL"]  # ticker prefix, shorter first
    assert U.search_assets(ASSETS, "palantir")[0]["symbol"] == "PLTR"  # company name
    assert U.search_assets(ASSETS, "s&p")[0]["symbol"] == "SPY"
    assert U.search_assets(ASSETS, "") == [] and U.search_assets(ASSETS, "zzzz") == []
    assert U.is_known_symbol(ASSETS, "pltr") and not U.is_known_symbol(ASSETS, "NOPE")


def test_asset_list_is_cached_and_survives_failures(tmp_path):
    path, calls = tmp_path / "assets.json", []

    def fetch():
        calls.append(1)
        return ASSETS

    assert U.load_assets(path, fetcher=fetch, now=1000.0) == ASSETS and calls == [1]
    assert U.load_assets(path, fetcher=fetch, now=1000.0 + 3600) == ASSETS and calls == [1]  # fresh: no refetch
    assert U.load_assets(path, fetcher=fetch, now=1000.0 + 3 * 86400) == ASSETS and calls == [1, 1]  # stale: refetch

    def boom():
        raise RuntimeError("offline")

    assert U.load_assets(path, fetcher=boom, now=1000.0 + 9 * 86400) == ASSETS  # stale cache beats nothing
    fallback = U.load_assets(tmp_path / "none.json", fetcher=boom)
    assert any(a["symbol"] == "AAPL" for a in fallback)  # never empty, so search always works


def test_watchlist_add_remove_cap_and_dupes(tmp_path):
    p = tmp_path / "wl.json"
    assert U.load_watchlist(p) == []
    assert U.add_to_watchlist(" pltr ", p) == (["PLTR"], None)
    assert U.add_to_watchlist("PLTR", p)[0] == ["PLTR"]
    for i in range(U.WATCHLIST_MAX):
        U.add_to_watchlist(f"T{i}", p)
    items, err = U.add_to_watchlist("EXTRA", p)
    assert len(items) == U.WATCHLIST_MAX and "full" in err
    assert "PLTR" not in U.remove_from_watchlist("pltr", p)
    p.write_text("not json")
    assert U.load_watchlist(p) == []


def test_ensure_symbol_data_loads_only_when_asked_and_tops_up(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path}/o.db")
    init_db(eng)
    full, calls = make_bars(n_days=10, seed=4), []

    def fetch(sym, tf, a, b):
        calls.append((sym, tf))
        return full

    assert latest_bar_time(eng, "PLTR", "15Min") is None  # nothing is stored until the symbol is opened
    r = ensure_symbol_data("pltr", engine=eng, fetch=fetch)
    assert r["new_symbol"] and r["error"] is None and r["bars"]["15Min"] == len(full)
    assert sorted(tf for _, tf in calls) == ["15Min", "5Min"]
    assert len(load_bars(eng, "PLTR", "15Min", extended_hours=True)) == len(full)
    calls.clear()
    r = ensure_symbol_data("PLTR", engine=eng, fetch=fetch)  # second open: top-up only
    assert not r["new_symbol"] and len(calls) == 2


def test_ensure_symbol_data_reports_an_unknown_ticker_instead_of_raising(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path}/o.db")
    r = ensure_symbol_data("ZZZZ", engine=eng, fetch=lambda *a: make_bars(n_days=1).iloc[0:0])
    assert r["error"] and "ZZZZ" in r["error"]

    def boom(*a):
        raise RuntimeError("API down")

    assert ensure_symbol_data("ZZZZ", engine=eng, fetch=boom)["error"]  # provider failure is reported too


def test_search_open_and_watchlist_flow_in_the_app(tmp_path, monkeypatch):
    """Search a ticker -> its bars load on demand -> the chart opens -> star it -> it joins the watchlist."""
    from streamlit.testing.v1 import AppTest

    import src.config.settings as cfg
    import src.data_ingestion.on_demand as od
    from src.data_ingestion.backfill import save_bars

    db = f"sqlite:///{tmp_path/'app.db'}"
    monkeypatch.setenv("DATABASE_URL", db)
    monkeypatch.setattr(U, "ASSETS_PATH", tmp_path / "assets.json")
    monkeypatch.setattr(U, "WATCHLIST_PATH", tmp_path / "wl.json")
    monkeypatch.setattr(U, "fetch_assets_from_alpaca", lambda: ASSETS)
    monkeypatch.setattr(U, "load_assets", lambda *a, **k: ASSETS)
    loads = []

    def fake_ensure(sym, engine=None, **k):
        loads.append(sym)
        save_bars(engine, sym, "15Min", make_bars(n_days=60, seed=8))
        return dict(symbol=sym, error=None, new_symbol=True, bars={})

    monkeypatch.setattr(od, "ensure_symbol_data", fake_ensure)
    cfg.get_settings.cache_clear()
    import streamlit as st

    st.cache_data.clear()
    st.cache_resource.clear()
    try:
        at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src" / "dashboard" / "app.py"), default_timeout=180).run()
        assert not at.exception, [e.value for e in at.exception]
        assert loads == ["SPY"] and at.session_state["sym"] == "SPY"  # one chart stays open: the first ticker on your list
        at.selectbox(key="search").select("PLTR").run()
        assert not at.exception, [e.value for e in at.exception]
        assert loads == ["SPY", "PLTR"] and at.session_state["sym"] == "PLTR"
        from src.execution import control
        from src.db.schema import get_engine
        assert "PLTR" not in control.trade_tickers(get_engine(db))  # searching never adds it to Trade control
        at.button(key="wl_add").click().run()
        assert json.loads((tmp_path / "wl.json").read_text()) == ["PLTR"]
        at.button(key="tl_add").click().run()
        assert "PLTR" in control.trade_tickers(get_engine(db)) and control.get_modes(get_engine(db))["PLTR"] == "off"
    finally:
        cfg.get_settings.cache_clear()
