"""Market data ingestion: Alpaca primary, yfinance fallback."""
from __future__ import annotations

from datetime import datetime

import pandas as pd


def get_bars_with_fallback(
    symbol: str, timeframe: str, start: datetime, end: datetime | None = None
) -> pd.DataFrame:
    """Try Alpaca first; fall back to yfinance if it raises, or returns nothing for a long window.

    A short window that comes back empty is a legitimate "no new bars yet" (the SIP delay), so it is NOT
    replaced with yfinance data (different source, slightly different prices and volume).
    """
    from datetime import timezone

    from src.data_ingestion import alpaca_data, yfinance_fallback

    try:
        df = alpaca_data.get_bars(symbol, timeframe, start, end)
        if len(df):
            return df
        s = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        e = (end or datetime.now(timezone.utc))
        e = e if e.tzinfo else e.replace(tzinfo=timezone.utc)
        if (e - s).total_seconds() <= 2 * 86400:
            return df
    except Exception as exc:  # noqa: BLE001
        print(f"[data] Alpaca failed for {symbol}: {exc}; falling back to yfinance")
    return yfinance_fallback.get_bars(symbol, timeframe, start, end)
