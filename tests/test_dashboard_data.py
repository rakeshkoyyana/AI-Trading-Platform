import json

import numpy as np
import pandas as pd
import pytest

from src.dashboard import data as D
from src.dashboard import theme as T
from src.dashboard.chart_component import chart_html
from src.data_ingestion.synthetic import make_bars
from src.smc_logic import compute_context


@pytest.fixture(scope="module")
def world():
    bars = make_bars(n_days=40, seed=5)
    return bars, compute_context(bars)


def test_resample_preserves_ohlcv_and_alignment(world):
    bars, _ = world
    h = D.resample_bars(bars, "1Hour")
    d = D.resample_bars(bars, "1Day")
    assert len(d) == 40
    assert h["volume"].sum() == pytest.approx(bars["volume"].sum())
    assert (h["high"] >= h[["open", "close"]].max(axis=1)).all() and (h["low"] <= h[["open", "close"]].min(axis=1)).all()
    first_day = bars.iloc[:26]
    assert d["open"].iat[0] == first_day["open"].iat[0] and d["close"].iat[0] == first_day["close"].iat[-1]
    assert d["high"].iat[0] == first_day["high"].max()
    # 09:30 ET anchored hourly buckets: 26 fifteen-minute bars/day -> 7 hourly buckets
    assert len(h) == 40 * 7
    assert len(D.resample_bars(bars, "2Hour")) == 40 * 4 and len(D.resample_bars(bars, "4Hour")) == 40 * 2
    assert D.resample_bars(bars, "15Min") is not None and len(D.resample_bars(bars, "15Min")) == len(bars)


def test_payload_is_json_and_well_formed(world):
    bars, ctx = world
    trades = pd.DataFrame([dict(symbol="AAA", entry_time=bars["timestamp"].iat[600], exit_time=bars["timestamp"].iat[610], direction="short",
                                entry_price=100.0, exit_price=98.0, qty=5, pnl=10.0, stop_loss=101.0, take_profit=97.0)])
    b5 = make_bars(n_days=10, seed=9, timeframe_minutes=5)
    p = D.build_chart_payload(bars, ctx, trades, "AAA", bars_5m=b5)
    json.dumps(p)  # must serialise
    assert list(p["datasets"]) == ["5m", "15m", "30m", "1H", "2H", "4H", "1D"]
    c = p["datasets"]["15m"]["candles"]
    assert [x["time"] for x in c] == sorted(x["time"] for x in c) and len(c) == len(bars)
    assert len(p["datasets"]["5m"]["candles"]) == len(b5)
    for tf, ds in p["datasets"].items():
        assert all(z["top"] > z["bottom"] for z in ds["zones"]), tf
    kinds = {m["kind"] for m in p["datasets"]["15m"]["markers"]}
    assert "signals" in kinds and {m["kind"] for m in p["tmarkers"]} == {"trades"}
    assert {s["label"] for s in p["tsegs"] if s["kind"] == "trade"} == {"Entry", "SL", "TP"}
    # signal markers line up with the context's events
    n_sig = int(np.isin(ctx["signal_event"].to_numpy(), ["long", "short"]).sum())
    assert sum(m["kind"] == "signals" for m in p["datasets"]["15m"]["markers"]) == n_sig
    # RSI/EMA lines carry no NaNs
    for k in ("ema9", "ema21", "rsi"):
        assert all(np.isfinite(x["value"]) for x in p["datasets"]["15m"][k])


def test_signals_are_computed_per_timeframe_not_copied_from_15m(world):
    """An hourly chart must show hourly-bar signals (as TradingView would), not the 15-minute ones."""
    from src.smc_logic import compute_context

    bars, _ = world
    p = D.build_chart_payload(bars, None, pd.DataFrame(columns=["symbol"]), "AAA")
    hourly = D.resample_bars(bars, "1Hour")
    want = int(np.isin(compute_context(hourly)["signal_event"].to_numpy(), ["long", "short"]).sum())
    got = sum(m["kind"] == "signals" for m in p["datasets"]["1H"]["markers"])
    assert got == want
    assert got != sum(m["kind"] == "signals" for m in p["datasets"]["15m"]["markers"])


def test_trade_overlays_can_be_attached_to_a_cached_payload(world):
    bars, _ = world
    base = D.build_chart_payload(bars.iloc[:400], None, pd.DataFrame(columns=["symbol"]), "AAA")
    assert base["tmarkers"] == [] and base["t_last"] > 0
    tr = pd.DataFrame([dict(symbol="AAA", entry_time=bars["timestamp"].iat[300], exit_time=None, direction="long", entry_price=100.0,
                            exit_price=None, qty=1, pnl=None, stop_loss=99.0, take_profit=102.0)])
    assert len(D.with_trades(base, tr)["tsegs"]) == 3 and base["tsegs"] == []


def _session_bars(days=2):
    """15-minute bars 04:00-20:00 ET for a few days (full extended session), UTC-naive timestamps."""
    rows = []
    for d in pd.bdate_range("2026-03-02", periods=days):  # winter: EST = UTC-5
        for k in range(64):
            t_et = pd.Timestamp(d) + pd.Timedelta(hours=4, minutes=15 * k)
            rows.append(dict(timestamp=(t_et + pd.Timedelta(hours=5)).tz_localize(None), open=100 + k, high=101 + k, low=99 + k, close=100.5 + k, volume=1000.0))
    return pd.DataFrame(rows)


def test_resample_is_session_aware_for_extended_hours():
    bars = _session_bars()

    def et(ts):
        return (pd.Timestamp(ts) - pd.Timedelta(hours=5)).strftime("%H:%M")

    for tf, expect in {
        "4Hour": ["04:00", "08:00", "09:30", "13:30", "16:00", "20:00"][:5],
        "2Hour": ["04:00", "06:00", "08:00", "09:30", "11:30", "13:30", "15:30", "16:00", "18:00"],
        "1Hour": ["04:00", "05:00", "06:00", "07:00", "08:00", "09:00", "09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30", "16:00", "17:00", "18:00", "19:00"],
    }.items():
        out = D.resample_bars(bars, tf)
        assert [et(t) for t in out["timestamp"].iloc[: len(expect)]] == expect, tf
        assert out["volume"].sum() == bars["volume"].sum()
    # the 09:30 regular bar never mixes pre-market prints
    h = D.resample_bars(bars, "1Hour")
    reg = h[h["timestamp"].map(et) == "09:30"].iloc[0]
    assert reg["open"] == bars.loc[(bars["timestamp"].map(et) == "09:30")].iloc[0]["open"]
    # daily bars are regular session only
    d = D.resample_bars(bars, "1Day")
    assert len(d) == 2 and d["volume"].iat[0] == 26 * 1000.0
    assert d["high"].iat[0] == bars.loc[bars["timestamp"].map(et).between("09:30", "15:45"), "high"].iloc[:26].max()


def test_fvg_zones_respect_mitigation(world):
    bars, ctx = world
    zones = [z for z in D.smc_overlays(ctx)["zones"] if z["kind"] == "fvg"]
    t_last = int(pd.Timestamp(ctx["timestamp"].iat[-1]).tz_localize("UTC").timestamp())
    for z in zones:
        assert z["t1"] >= z["t0"]
        assert z["active"] == (z["t1"] == t_last)


def test_scan_symbol_confluence_is_bounded_and_explained(world):
    bars, ctx = world
    r = D.scan_symbol("AAA", bars, ctx, {"score": 0.3, "n": 2})
    assert 0 <= r["confluence"] <= r["confluence_max"] == len(r["checks"]) == 6
    assert r["confluence"] == sum(r["checks"].values())
    assert r["bias"] in ("long", "short") and r["sentiment"] == 0.3
    assert D.scan_symbol("X", bars.iloc[0:0], None)["price"] is None
    thin = D.scan_symbol("AAA", bars.iloc[:30], None)
    assert thin["confluence"] is None and thin["signal"] == "—"


def test_chart_html_inlines_everything_and_escapes_script_tags(world):
    bars, ctx = world
    p = D.build_chart_payload(bars, ctx, pd.DataFrame(columns=["symbol"]), "A</script><b>")
    html = chart_html(p)
    assert "LightweightCharts" in html and "__PAYLOAD__" not in html and "/*__LIB__*/" not in html
    assert "A</script><b>" not in html  # payload cannot break out of the script tag
    assert html.count("<script>") == html.count("</script>")


def test_theme_helpers_escape_and_render():
    html = T.news_list([dict(symbol="S", headline="<img src=x onerror=alert(1)>", url="javascript:alert(1)", score=0.5,
                                          source="x", timestamp=pd.Timestamp("2026-01-01").to_pydatetime())])
    assert "&lt;img" in html and "javascript:" not in html and 'href="#"' in html
    assert T.sent_chip(0.6).count("Bullish") == 1 and "Bearish" in T.sent_chip(-0.6) and "no news" in T.sent_chip(None)
    assert T.spark_svg([1, 2, 3]).startswith("<svg") and T.spark_svg([1]) == ""
    tape = T.ticker_tape([dict(symbol="SPY", price=500.0, chg_pct=-1.0)], [dict(symbol="SPY", headline="Hello", score=0.4)])
    assert tape.count("SPY") >= 2 and "▼" in tape
    assert T.money(-5) == "-$5.00" and T.money(5, True) == "+$5.00" and T.pct(None) == "—"
