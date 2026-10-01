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
    assert D.resample_bars(bars, "15Min") is not None and len(D.resample_bars(bars, "15Min")) == len(bars)


def test_payload_is_json_and_well_formed(world):
    bars, ctx = world
    trades = pd.DataFrame([dict(symbol="AAA", entry_time=bars["timestamp"].iat[600], exit_time=bars["timestamp"].iat[610], direction="short",
                                entry_price=100.0, exit_price=98.0, qty=5, pnl=10.0, stop_loss=101.0, take_profit=97.0)])
    p = D.build_chart_payload(bars, ctx, trades, "AAA")
    json.dumps(p)  # must serialise
    assert set(p["datasets"]) == {"15m", "30m", "1H", "1D"}
    c = p["datasets"]["15m"]["candles"]
    assert [x["time"] for x in c] == sorted(x["time"] for x in c) and len(c) == len(bars)
    assert all(z["top"] > z["bottom"] for z in p["zones"])
    kinds = {m["kind"] for m in p["markers"]}
    assert {"signals", "trades"} <= kinds
    assert {s["label"] for s in p["segs"] if s["kind"] == "trade"} == {"Entry", "SL", "TP"}
    # signal markers line up with the context's events
    n_sig = int(np.isin(ctx["signal_event"].to_numpy(), ["long", "short"]).sum())
    assert sum(m["kind"] == "signals" for m in p["markers"]) == n_sig
    # RSI/EMA lines carry no NaNs
    for k in ("ema9", "ema21", "rsi"):
        assert all(np.isfinite(x["value"]) for x in p["datasets"]["15m"][k])


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
