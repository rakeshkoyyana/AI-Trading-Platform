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

    # incremental run asks from the newest stored bar (re-fetching it, since it may have been saved half-formed)
    backfill(["AAA"], "15Min", incremental=True, engine=engine, fetch=fake_fetch)
    assert calls[-1].replace(tzinfo=None) == full["timestamp"].iloc[-1].to_pydatetime()
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


def test_extended_hours_are_stored_and_filtered_only_on_read(tmp_path):
    from src.data_ingestion.backfill import load_bars, save_bars
    from src.db.schema import get_engine, init_db

    # EDT: 08:00 ET (pre), 09:30 ET (open), 15:45 ET (last RTH bar), 16:00 ET (post) -> UTC +4h
    ts = pd.to_datetime(["2026-07-01 12:00", "2026-07-01 13:30", "2026-07-01 19:45", "2026-07-01 20:00"])
    df = pd.DataFrame({"timestamp": ts, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0})
    eng = get_engine(f"sqlite:///{tmp_path/'x.db'}")
    init_db(eng)
    save_bars(eng, "AAA", "15Min", df)
    assert len(load_bars(eng, "AAA", "15Min", extended_hours=True)) == 4
    rth = load_bars(eng, "AAA", "15Min", extended_hours=False)
    assert list(rth["timestamp"].dt.strftime("%H:%M")) == ["13:30", "19:45"]
    assert len(load_bars(eng, "AAA", "15Min")) == 4  # default follows settings (True)


def test_refetching_a_bar_overwrites_a_half_formed_one(engine):
    df = make_bars(n_days=2)
    partial = df.copy()
    partial.loc[partial.index[-1], ["high", "close", "volume"]] = [df["low"].iat[-1], df["low"].iat[-1], 1.0]
    save_bars(engine, "AAA", "15Min", partial)
    save_bars(engine, "AAA", "15Min", df)  # the completed bar arrives on the next fetch
    got = load_bars(engine, "AAA", "15Min", extended_hours=True)
    assert len(got) == len(df)
    assert got["volume"].iat[-1] == df["volume"].iat[-1] and got["high"].iat[-1] == df["high"].iat[-1]


def test_effective_end_clamps_to_the_data_horizon():
    from datetime import datetime, timedelta, timezone

    from src.data_ingestion.common import effective_end

    now = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)
    assert effective_end(None, 16, now) == now - timedelta(minutes=16)
    assert effective_end(now, 16, now) == now - timedelta(minutes=16)
    early = now - timedelta(days=3)
    assert effective_end(early, 16, now) == early  # an earlier end is left alone
    assert effective_end(None, 0, now) == now


def test_fallback_only_for_errors_or_long_empty_windows(monkeypatch):
    from datetime import datetime, timedelta, timezone

    import pandas as pd

    from src.data_ingestion import alpaca_data, get_bars_with_fallback, yfinance_fallback
    from src.data_ingestion.common import BAR_COLUMNS

    empty = pd.DataFrame(columns=BAR_COLUMNS)
    yf_calls = []
    monkeypatch.setattr(alpaca_data, "get_bars", lambda *a, **k: empty)
    monkeypatch.setattr(yfinance_fallback, "get_bars", lambda *a, **k: yf_calls.append(1) or empty)
    now = datetime.now(timezone.utc)
    get_bars_with_fallback("X", "15Min", now - timedelta(hours=1), now)  # short window: legitimately empty
    assert yf_calls == []
    get_bars_with_fallback("X", "15Min", now - timedelta(days=30), now)  # long window empty: ask the fallback
    assert yf_calls == [1]


def test_data_delay_follows_the_feed():
    from src.config.settings import Settings

    assert Settings().alpaca_data_feed == "sip" and Settings().data_delay_minutes == 16
    assert Settings(alpaca_data_feed="iex").data_delay_minutes == 0
    assert Settings(sip_delay_minutes=0).data_delay_minutes == 0
