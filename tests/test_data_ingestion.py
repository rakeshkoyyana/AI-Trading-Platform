import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.data_ingestion.backfill import backfill, latest_bar_time, load_bars, save_bars
from src.data_ingestion.common import BAR_COLUMNS, standardize_bars
from src.data_ingestion.synthetic import make_bars
from src.db.schema import get_engine, init_db


@pytest.fixture()
def engine():
    eng = get_engine("sqlite:///:memory:")
    init_db(eng)
    return eng


def test_standardize_bars_shape_and_cleaning():
    raw = make_bars(n_days=3)
    raw.loc[5, "close"] = float("nan")
    dup = pd.concat([raw, raw.iloc[[0]]])
    out = standardize_bars(dup)
    assert list(out.columns) == BAR_COLUMNS
    assert not out[["open", "high", "low", "close"]].isna().any().any()
    assert out["timestamp"].is_monotonic_increasing
    assert out["timestamp"].is_unique
    assert out["timestamp"].dt.tz is None  # naive UTC


def test_synthetic_bars_are_valid_ohlc():
    df = make_bars(n_days=10)
    assert len(df) == 10 * 26
    assert (df["high"] >= df[["open", "close"]].max(axis=1) - 1e-9).all()
    assert (df["low"] <= df[["open", "close"]].min(axis=1) + 1e-9).all()


def test_save_is_idempotent(engine):
    df = make_bars(n_days=5)
    save_bars(engine, "TEST", "15Min", df)
    save_bars(engine, "TEST", "15Min", df)  # second insert must not duplicate
    assert len(load_bars(engine, "TEST", "15Min")) == len(df)


def test_backfill_and_incremental(engine):
    full = make_bars(n_days=20)
    calls = []

    def fake_fetch(sym, tf, start, end):
        calls.append(start)
        return full

    res = backfill(["AAA", "BBB"], "15Min", months=1, engine=engine, fetch=fake_fetch)
    assert res == {"AAA": len(full), "BBB": len(full)}
    assert latest_bar_time(engine, "AAA", "15Min") == full["timestamp"].iloc[-1]

    # incremental run should ask only for data after the latest stored bar
    backfill(["AAA"], "15Min", incremental=True, engine=engine, fetch=fake_fetch)
    assert calls[-1].replace(tzinfo=None) > full["timestamp"].iloc[-1].to_pydatetime()
    assert len(load_bars(engine, "AAA", "15Min")) == len(full)


def test_backfill_survives_provider_failure(engine):
    def boom(*a, **k):
        raise RuntimeError("provider down")

    assert backfill(["AAA"], "15Min", engine=engine, fetch=boom) == {"AAA": 0}


@pytest.mark.skipif(
    not (os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_SECRET_KEY")),
    reason="needs live Alpaca keys",
)
def test_live_alpaca_get_bars():
    from src.data_ingestion.alpaca_data import get_bars

    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    df = get_bars("SPY", "15Min", end - timedelta(days=7), end)
    assert len(df) > 0
    assert list(df.columns) == BAR_COLUMNS
    assert not df[["open", "high", "low", "close"]].isna().any().any()


def test_regular_hours_filter_and_purge(tmp_path):
    from src.data_ingestion.backfill import load_bars, purge_extended_hours, save_bars
    from src.data_ingestion.common import keep_regular_hours
    from src.db.schema import get_engine, init_db

    # EDT day: 08:00 ET (pre), 09:30 ET (open), 15:45 ET (last bar), 16:00 ET (post) -> UTC +4h
    ts = pd.to_datetime(["2026-07-01 12:00", "2026-07-01 13:30", "2026-07-01 19:45", "2026-07-01 20:00"])
    df = pd.DataFrame({"timestamp": ts, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0})
    kept = keep_regular_hours(df, "15Min")
    assert list(kept["timestamp"].dt.strftime("%H:%M")) == ["13:30", "19:45"]
    assert len(keep_regular_hours(df, "1Day")) == 4  # daily bars untouched

    eng = get_engine(f"sqlite:///{tmp_path/'x.db'}")
    init_db(eng)
    save_bars(eng, "AAA", "15Min", df)
    assert purge_extended_hours(eng) == 2
    assert len(load_bars(eng, "AAA", "15Min")) == 2
