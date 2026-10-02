"""Deterministic synthetic OHLCV generator for tests and dashboard demos.

Generates regular-session bars (09:30-16:00 ET) on weekdays with trending
regimes, so structure/EMA/RSI logic has something realistic to chew on.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def make_bars(
    n_days: int = 60,
    timeframe_minutes: int = 15,
    start: str = "2026-06-01",
    start_price: float = 100.0,
    seed: int = 7,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start=start, periods=n_days, tz="America/New_York")
    per_day = int(6.5 * 60 / timeframe_minutes)

    stamps = []
    for d in days:
        open_ts = d.replace(hour=9, minute=30)
        stamps.extend(open_ts + pd.Timedelta(minutes=timeframe_minutes * i) for i in range(per_day))
    idx = pd.DatetimeIndex(stamps).tz_convert("UTC")

    n = len(idx)
    # regime-switching drift so we get genuine trends + reversals
    regime = np.repeat(rng.choice([-1, 0, 1], size=n // 80 + 2, p=[0.3, 0.3, 0.4]), 80)[:n]
    drift = regime * 0.0004
    vol = 0.0035
    rets = drift + rng.normal(0, vol, n)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_price], close[:-1]])
    spread = np.abs(rng.normal(0, vol * 0.8, n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    base_vol = rng.lognormal(mean=11, sigma=0.35, size=n)
    spikes = rng.random(n) < 0.12
    volume = base_vol * np.where(spikes, rng.uniform(1.5, 3.0, n), 1.0)

    return pd.DataFrame(
        {
            "timestamp": idx.tz_localize(None),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume.round(),
        }
    )
