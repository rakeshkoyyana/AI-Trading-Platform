import numpy as np
import pandas as pd
import pytest

from src.data_ingestion.synthetic import make_bars
from src.smc_logic import compute_context
from src.smc_logic.config import SMCConfig
from src.smc_logic.fvg import detect_fvg
from src.smc_logic.indicators import atr, ema, rma, rsi, sma, true_range
from src.smc_logic.label import label_signals
from src.smc_logic.levels import stop_target_levels
from src.smc_logic.order_blocks import detect_order_blocks
from src.smc_logic.structure import StructureResult, detect_structure
from src.smc_logic.triple_confirmation import (
    get_signal,
    signal_events,
    simulate_strategy,
    triple_confirmation_frame,
)
from src.smc_logic.zones import detect_premium_discount


# ---------------------------------------------------------------- indicators
def test_ema_is_sma_seeded_like_pine():
    out = ema(np.array([1, 2, 3, 4, 5, 6], float), 3)
    assert np.isnan(out[:2]).all()
    assert out[2] == pytest.approx(2.0)  # SMA(1,2,3)
    assert out[3] == pytest.approx(0.5 * 4 + 0.5 * 2.0)  # alpha = 2/(3+1)
    assert out[4] == pytest.approx(0.5 * 5 + 0.5 * out[3])


def test_rma_and_sma():
    x = np.array([2, 4, 6, 8, 10], float)
    assert sma(x, 3)[2] == pytest.approx(4.0)
    r = rma(x, 3)
    assert r[2] == pytest.approx(4.0)
    assert r[3] == pytest.approx((4.0 * 2 + 8) / 3)


def test_rsi_matches_independent_wilder_loop():
    rng = np.random.default_rng(1)
    close = 100 + np.cumsum(rng.normal(0, 1, 200))
    got = rsi(close, 14)
    # independent reference implementation
    ch = np.diff(close)
    up, dn = np.maximum(ch, 0), np.maximum(-ch, 0)
    au, ad = up[:14].mean(), dn[:14].mean()
    ref = {14: 100 - 100 / (1 + au / ad)}
    for i in range(14, len(ch)):
        au = (au * 13 + up[i]) / 14
        ad = (ad * 13 + dn[i]) / 14
        ref[i + 1] = 100 - 100 / (1 + au / ad)
    assert np.isnan(got[:14]).all()
    for k, v in ref.items():
        assert got[k] == pytest.approx(v, abs=1e-9)


def test_rsi_extremes():
    assert rsi(np.arange(1.0, 40.0), 14)[-1] == 100.0
    assert rsi(np.arange(40.0, 1.0, -1), 14)[-1] == 0.0


def test_true_range_first_bar_is_high_minus_low():
    tr = true_range(np.array([10.0, 12]), np.array([8.0, 9]), np.array([9.0, 11]))
    assert tr[0] == 2.0
    assert tr[1] == 3.0  # max(12-9, |12-9|, |9-9|)


# ---------------------------------------------------------------- causality
def test_no_lookahead_context_is_causal():
    df = make_bars(n_days=40, seed=11)
    full = compute_context(df)
    cols = [
        "swing_bull_bos", "swing_bear_choch", "int_bull_bos", "int_bear_choch", "eqh", "eql",
        "swing_trend", "internal_trend", "trail_top", "trail_bottom", "zone",
        "ob_bull_count", "ob_bear_count", "fvg_bull_active", "signal", "signal_event", "rsi",
    ]
    for k in (400, 777, 1000):
        part = compute_context(df.iloc[:k])
        pd.testing.assert_frame_equal(
            part[cols].reset_index(drop=True),
            full[cols].iloc[:k].reset_index(drop=True),
            check_dtype=False,
        )


# ---------------------------------------------------------------- structure
def _zigzag(legs, step=1.0, base=100.0, bars_per_leg=8):
    """Build OHLC from a list of leg end-prices (e.g. [110, 100, 120, 95])."""
    closes, prev = [], base
    for end in legs:
        closes.extend(np.linspace(prev, end, bars_per_leg, endpoint=False))
        prev = end
    closes.append(prev)
    c = np.array(closes)
    o = np.concatenate([[base], c[:-1]])
    hi = np.maximum(o, c) + 0.2
    lo = np.minimum(o, c) - 0.2
    ts = pd.date_range("2026-01-05 14:30", periods=len(c), freq="15min")
    return pd.DataFrame({"timestamp": ts, "open": o, "high": hi, "low": lo, "close": c, "volume": 1e5})


SMALL = SMCConfig(swing_length=4, internal_length=2, equal_length=2)


def test_bos_then_choch_tagging_on_zigzag():
    df = _zigzag([110, 100, 120, 90, 125, 85])
    st = detect_structure(df, SMALL)
    ev = [e for e in st.events if not e["internal"]]
    assert ev, "expected swing structure breaks on a clean zigzag"
    last_dir = 0
    for e in ev:
        if e["tag"] == "choch":
            assert last_dir == -e["bias"], "CHoCH must flip the prevailing bias"
        else:
            assert last_dir in (0, e["bias"]), "BOS must continue the prevailing bias"
        last_dir = e["bias"]
    assert any(e["tag"] == "choch" for e in ev)


def test_structure_flags_match_events():
    df = make_bars(n_days=30, seed=5)
    st = detect_structure(df)
    f = st.frame
    for e in st.events:
        pre = "int" if e["internal"] else "swing"
        side = "bull" if e["bias"] == 1 else "bear"
        assert f[f"{pre}_{side}_{e['tag']}"].iat[e["i"]]
    flag_cols = [c for c in f.columns if c.startswith(("int_b", "swing_b"))]
    assert int(f[flag_cols].to_numpy().sum()) == len(st.events)


def test_each_pivot_breaks_at_most_once():
    df = make_bars(n_days=60, seed=2)
    st = detect_structure(df)
    seen = set()
    for e in st.events:
        key = (e["internal"], e["bias"], e["pivot_bar"], e["level"])
        assert key not in seen
        seen.add(key)


# ------------------------------------------------------------- order blocks
def _manual_structure(df, events):
    f = pd.DataFrame(
        {"parsed_high": df["high"].to_numpy(), "parsed_low": df["low"].to_numpy()}, index=df.index
    )
    return StructureResult(frame=f, events=events)


def test_bullish_order_block_is_lowest_parsed_low_and_gets_mitigated():
    lows = [10, 9, 7, 8, 9, 10, 11, 12, 6, 12]
    df = pd.DataFrame(
        {
            "open": [11] * 10,
            "high": [x + 2 for x in lows],
            "low": lows,
            "close": [x + 1 for x in lows],
            "volume": 1.0,
        }
    )
    df.loc[7, "close"] = 14.0  # price sits above the OB before the sweep
    ev = [dict(i=5, internal=True, bias=1, tag="bos", pivot_bar=0, level=0.0)]
    ob = detect_order_blocks(df, SMCConfig(), structure=_manual_structure(df, ev))
    # OB = bar with min low in [0, 5) -> bar 2 (low 7, high 9)
    assert ob["bull_ob_low"].iat[5] == 7 and ob["bull_ob_high"].iat[5] == 9
    assert ob["ob_bull_count"].iat[5] == 1
    # bar 8 low (6) < OB low (7) -> mitigated that bar
    assert ob["ob_mit_int_bull"].iat[8] and ob["ob_bull_count"].iat[8] == 0
    assert ob["ob_bull_count"].iat[7] == 1


def test_swing_order_blocks_respect_config_flag():
    df = make_bars(n_days=40, seed=9)
    st = detect_structure(df)
    off = detect_order_blocks(df, SMCConfig(internal_order_blocks=False, swing_order_blocks=False), st)
    assert off["ob_bull_count"].max() == 0 and off["ob_bear_count"].max() == 0
    on = detect_order_blocks(df, SMCConfig(internal_order_blocks=True), st)
    assert on["ob_bull_count"].max() > 0


# ---------------------------------------------------------------------- fvg
def test_bullish_fvg_detected_and_removed_when_filled():
    base = dict(volume=1.0)
    bars = [
        (10.0, 10.5, 9.5, 10.0),  # 0
        (10.0, 10.8, 9.9, 10.2),  # 1
        (10.2, 11.0, 10.0, 10.9),  # 2: high[2]=11.0 is the gap floor
        (11.0, 12.5, 11.3, 12.4),  # 3 big bull candle (close[1] context for bar 4)
        (12.4, 13.0, 12.0, 12.9),  # 4: low 12.0 > high[2]=11.0? bar4 uses bars 2,3 -> need low[4] > high[2]
    ]
    df = pd.DataFrame(bars, columns=["open", "high", "low", "close"]).assign(**base)
    f = detect_fvg(df, SMCConfig(fvg_auto_threshold=False))
    # bar 3 gaps over bar 1 (low 11.3 > high[1] 10.8) and bar 4 gaps over bar 2 (low 12.0 > high[2] 11.0)
    assert f["fvg_bull_new"].iat[3] and f["fvg_bull_new"].iat[4]
    assert not f["fvg_bear_new"].any()
    assert f["fvg_bull_active"].iat[4] == 2
    # price trades down through both gap floors (10.8 and 11.0) -> both removed
    df2 = pd.concat(
        [df, pd.DataFrame([(12.0, 12.2, 10.5, 10.8)], columns=["open", "high", "low", "close"]).assign(**base)],
        ignore_index=True,
    )
    f2 = detect_fvg(df2, SMCConfig(fvg_auto_threshold=False))
    assert f2["fvg_bull_active"].iat[5] == 0


# -------------------------------------------------------------------- zones
def test_premium_discount_classification():
    frame = pd.DataFrame({"trail_top": [200.0] * 5, "trail_bottom": [100.0] * 5})
    close = pd.Series([199.0, 101.0, 150.0, 170.0, 130.0])
    z = detect_premium_discount(frame, close)
    assert list(z["zone"]) == ["premium", "discount", "equilibrium", "above_eq", "below_eq"]
    assert z["pd_position"].iat[0] == pytest.approx(0.99)


def test_zone_unknown_before_first_swing():
    f = pd.DataFrame({"trail_top": [np.nan], "trail_bottom": [np.nan]})
    assert detect_premium_discount(f, pd.Series([100.0]))["zone"].iat[0] == "unknown"


# ------------------------------------------------------ triple confirmation
def test_conditions_follow_pine_definitions_exactly():
    df = make_bars(n_days=60, seed=4)
    f = triple_confirmation_frame(df)
    r, fast, slow = f["rsi"], f["ema_fast"], f["ema_slow"]
    spike = df["volume"] > df["volume"].rolling(20).mean() * 1.2
    long_ref = (fast > slow) & (r > 50) & (r < 70) & spike
    short_ref = (fast < slow) & (r < 50) & (r > 30) & spike
    assert (f["long_cond"] == long_ref).all()
    assert (f["short_cond"] == short_ref).all()
    assert not (f["long_cond"] & f["short_cond"]).any()
    assert (f["exit_long"] == ((fast < slow) | (r >= 70))).all()
    assert (f["exit_short"] == ((fast > slow) | (r <= 30))).all()


def test_signal_events_are_rising_edges_only():
    df = make_bars(n_days=60, seed=6)
    sig, ev = get_signal(df), signal_events(df)
    for i in np.where(ev != "none")[0]:
        assert sig.iat[i] == ev.iat[i]
        assert i == 0 or sig.iat[i - 1] != sig.iat[i]
    assert (ev != "none").sum() > 0


def test_volume_filter_off_gives_more_signals():
    df = make_bars(n_days=60, seed=6)
    on = (get_signal(df, SMCConfig(volume_filter=True)) != "none").sum()
    off = (get_signal(df, SMCConfig(volume_filter=False)) != "none").sum()
    assert off > on


def test_simulated_strategy_fills_next_open_and_never_overlaps():
    df = make_bars(n_days=80, seed=8)
    t = simulate_strategy(df)
    assert len(t) > 3
    for tr in t.itertuples():
        fill = tr.signal_bar + 1
        assert tr.entry_time == df["timestamp"].iat[fill]
        assert tr.entry_price == df["open"].iat[fill]
    closed = t.dropna(subset=["exit_time"])
    assert (closed["exit_time"] >= closed["entry_time"]).all()
    assert (t["entry_time"].iloc[1:].to_numpy() >= closed["exit_time"].iloc[: len(t) - 1].to_numpy()[: len(t) - 1]).all()


# ---------------------------------------------------------- levels + labels
def test_stop_target_uses_order_block_and_min_rr():
    row = pd.Series(dict(close=100.0, atr=1.0, bull_ob_low=98.0, swing_high=100.5, bear_ob_low=np.nan))
    lv = stop_target_levels(row, "long")
    assert lv.stop_source == "order_block" and lv.stop == pytest.approx(98.0 - 0.1)
    # structural target (100.5) is closer than 1.5R -> pushed out to min RR
    assert lv.target == pytest.approx(100.0 + 1.5 * lv.risk) and lv.target_source == "rr"


def test_stop_target_short_falls_back_to_atr():
    row = pd.Series(dict(close=50.0, atr=2.0))
    lv = stop_target_levels(row, "short")
    assert lv.stop_source == "atr" and lv.stop == pytest.approx(53.0)
    assert lv.target < 50.0


def _label_ctx(path):
    n = len(path)
    ctx = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-05", periods=n, freq="15min"),
            "open": [p[0] for p in path],
            "high": [p[1] for p in path],
            "low": [p[2] for p in path],
            "close": [p[3] for p in path],
            "atr": 1.0,
            "signal_event": ["none"] * n,
        }
    )
    ctx.loc[0, "signal_event"] = "long"
    return ctx


def test_label_stop_target_and_horizon():
    flat = (100.0, 100.2, 99.8, 100.0)
    # long, entry 100, ATR stop = 98.5, target = 100 + 1.5*1.5 = 102.25
    hit_target = _label_ctx([flat, flat, (100, 103, 99.9, 102.5)] + [flat] * 5)
    lab = label_signals(hit_target, horizon=4)
    assert lab["exit_reason"].iat[0] == "target" and lab["label_win"].iat[0] == 1

    hit_stop = _label_ctx([flat, flat, (100, 100.1, 98.0, 98.5)] + [flat] * 5)
    lab = label_signals(hit_stop, horizon=4)
    assert lab["exit_reason"].iat[0] == "stop" and lab["label_win"].iat[0] == 0

    both = _label_ctx([flat, flat, (100, 103, 98.0, 100)] + [flat] * 5)
    assert label_signals(both, horizon=4)["exit_reason"].iat[0] == "stop"  # conservative

    near_end = _label_ctx([flat, flat, flat])
    assert np.isnan(label_signals(near_end, horizon=4)["label_win"].iat[0])
