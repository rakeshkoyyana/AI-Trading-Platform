"""Alpaca market data loader (IEX feed, free tier)."""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from src.config import get_settings
from src.data_ingestion.common import BAR_COLUMNS, keep_regular_hours, standardize_bars


def _alpaca_timeframe(timeframe: str):
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    mapping = {
        "1Min": TimeFrame(1, TimeFrameUnit.Minute),
        "5Min": TimeFrame(5, TimeFrameUnit.Minute),
        "15Min": TimeFrame(15, TimeFrameUnit.Minute),
        "30Min": TimeFrame(30, TimeFrameUnit.Minute),
        "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
        "1Day": TimeFrame(1, TimeFrameUnit.Day),
    }
    if timeframe not in mapping:
        raise ValueError(f"Unsupported timeframe {timeframe!r}; use one of {list(mapping)}")
    return mapping[timeframe]


def get_bars(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Return DataFrame[timestamp, open, high, low, close, volume] (UTC, naive).

    Raises if credentials are missing or the API call fails, so callers can fall
    back to yfinance explicitly rather than silently getting empty data.
    """
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest

    s = get_settings()
    if not s.alpaca_api_key or not s.alpaca_secret_key:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")

    client = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=_alpaca_timeframe(timeframe),
        start=start,
        end=end,
        feed=DataFeed.IEX,  # free tier
        adjustment="split",
    )
    bars = client.get_stock_bars(req).df
    if bars is None or len(bars) == 0:
        return pd.DataFrame(columns=BAR_COLUMNS)

    # alpaca-py returns a (symbol, timestamp) MultiIndex
    bars = bars.reset_index()
    if "symbol" in bars.columns:
        bars = bars[bars["symbol"] == symbol]
    return keep_regular_hours(standardize_bars(bars), timeframe)
