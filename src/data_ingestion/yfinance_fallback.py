"""yfinance fallback loader with the same signature as alpaca_data.get_bars.

Note: Yahoo only serves ~60 days of 5/15-minute history and may be delayed.
It is a redundancy path, not the primary source.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from src.data_ingestion.common import BAR_COLUMNS, YF_INTERVAL, keep_regular_hours, standardize_bars


def get_bars(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime | None = None,
) -> pd.DataFrame:
    import yfinance as yf

    if timeframe not in YF_INTERVAL:
        raise ValueError(f"Unsupported timeframe {timeframe!r}")

    raw = yf.download(
        symbol,
        start=start,
        end=end,
        interval=YF_INTERVAL[timeframe],
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=BAR_COLUMNS)

    if isinstance(raw.columns, pd.MultiIndex):  # newer yfinance: (field, ticker)
        raw.columns = raw.columns.get_level_values(0)
    raw = raw.reset_index()
    raw = raw.rename(columns={"Datetime": "timestamp", "Date": "timestamp"})
    return keep_regular_hours(standardize_bars(raw), timeframe)
