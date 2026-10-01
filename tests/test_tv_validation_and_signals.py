"""Self-consistency tests for the TradingView validator and the signals backfill."""
import numpy as np
import pandas as pd

from src.data_ingestion.backfill import save_bars
from src.data_ingestion.synthetic import make_bars
from src.db.schema import Signal, get_engine, init_db, session_scope
from src.smc_logic import compute_context
from src.smc_logic.backfill_signals import backfill_signals
from src.smc_logic.tv_validation import format_report, load_tradingview_csv, validate
from sqlalchemy import select


def _fake_tv_export(df: pd.DataFrame, iso: bool = True) -> pd.DataFrame:
    """Build a CSV-shaped frame the way TradingView would, from our own context."""
    ctx = compute_context(df)
    t = df["timestamp"]
    out = pd.DataFrame(
        {
            "time": t.dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            if iso
            else t.astype("datetime64[s]").astype("int64"),
            "open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"],
            "Volume": df["volume"],
        }
    )
    for tv, py in {
        "swing_bull_bos": "swing_bull_bos", "swing_bear_choch": "swing_bear_choch",
        "int_bull_bos": "int_bull_bos", "int_bear_choch": "int_bear_choch",
        "long_cond": "long_cond", "short_cond": "short_cond",
    }.items():
        out[tv] = ctx[py].astype(int)
    out["ema_fast"], out["rsi"] = ctx["ema_fast"], ctx["rsi"]
    out["swing_high_lvl"] = ctx["swing_high"]
    return out


def test_validator_perfect_on_self_export_iso_and_unix():
    df = make_bars(n_days=60, seed=21)
    for iso in (True, False):
        res = validate(_fake_tv_export(df, iso=iso))
        assert res["overall_agreement"] == 1.0
        assert all(r.agreement == 1.0 for r in res["events"])
        assert all(v["max_abs_diff"] < 1e-9 for v in res["values"].values())
        assert "PASS" in format_report(res)


def test_validator_detects_shifted_events():
    df = make_bars(n_days=60, seed=21)
    exp = _fake_tv_export(df)
    exp["int_bull_bos"] = exp["int_bull_bos"].shift(1, fill_value=0)  # TV one bar late
    res = validate(exp)
    r = next(x for x in res["events"] if x.name == "int_bull_bos")
    assert r.agreement < 0.2 and r.agreement_1bar > 0.9  # off by one bar is visible, and tolerated in ±1


def test_load_requires_ohlcv():
    bad = pd.DataFrame({"time": [1, 2], "open": [1, 1]})
    try:
        load_tradingview_csv(bad)
    except ValueError as e:
        assert "missing" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_backfill_signals_writes_labelled_rows_and_is_idempotent():
    eng = get_engine("sqlite:///:memory:")
    init_db(eng)
    save_bars(eng, "TEST", "15Min", make_bars(n_days=120, seed=3))
    res1 = backfill_signals(["TEST"], "15Min", horizon=8, engine=eng)
    assert res1["TEST"] > 20
    with session_scope(eng) as s:
        rows = s.execute(select(Signal)).scalars().all()
        n1 = len(rows)
        labelled = [r for r in rows if r.label_win is not None]
        assert len(labelled) > 0.9 * n1
        r = labelled[0]
        assert r.direction in {"long", "short"} and r.entry_price > 0
        assert "rsi" in r.confirmation_details_json and "zone" in r.confirmation_details_json
    backfill_signals(["TEST"], "15Min", horizon=8, engine=eng)  # re-run must not duplicate
    with session_scope(eng) as s:
        assert len(s.execute(select(Signal)).scalars().all()) == n1


def test_backfill_signals_skips_short_history():
    eng = get_engine("sqlite:///:memory:")
    init_db(eng)
    save_bars(eng, "TINY", "15Min", make_bars(n_days=3))
    assert backfill_signals(["TINY"], "15Min", engine=eng) == {"TINY": 0}
