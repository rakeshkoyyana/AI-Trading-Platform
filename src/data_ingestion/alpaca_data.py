"""Alpaca market data loader. Default feed is SIP (consolidated, matches TradingView); IEX is optional."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src.config import get_settings
from src.data_ingestion.common import BAR_COLUMNS, effective_end, standardize_bars


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
    feed: str | None = None,
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

    feed = (feed or s.alpaca_data_feed).lower()
    # SIP on the free plan: nothing newer than ~15 min. IEX is real-time, so no clamp (live tail).
    end = effective_end(end, s.data_delay_minutes if feed == "sip" else 0)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if start >= end:
        return pd.DataFrame(columns=BAR_COLUMNS)  # nothing available yet: a legitimate empty answer

    client = StockHistoricalDataClient(s.alpaca_api_key, s.alpaca_secret_key)
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=_alpaca_timeframe(timeframe),
        start=start,
        end=end,
        feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX,
        adjustment="split",
    )
    bars = client.get_stock_bars(req).df
    if bars is None or len(bars) == 0:
        return pd.DataFrame(columns=BAR_COLUMNS)

    # alpaca-py returns a (symbol, timestamp) MultiIndex
    bars = bars.reset_index()
    if "symbol" in bars.columns:
        bars = bars[bars["symbol"] == symbol]
    return standardize_bars(bars)
