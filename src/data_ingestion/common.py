"""Shared helpers so Alpaca and yfinance loaders return the identical shape."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

BAR_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]

# Canonical timeframe names used across the project -> provider specifics.
TIMEFRAME_MINUTES = {"1Min": 1, "5Min": 5, "15Min": 15, "30Min": 30, "1Hour": 60, "1Day": 1440}
YF_INTERVAL = {
    "1Min": "1m",
    "5Min": "5m",
    "15Min": "15m",
    "30Min": "30m",
    "1Hour": "60m",
    "1Day": "1d",
}


def standardize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Force a bars frame into [timestamp, open, high, low, close, volume].

    - timestamps become tz-naive UTC
    - sorted ascending, de-duplicated
    - rows with NaN OHLC dropped
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=BAR_COLUMNS)

    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    missing = [c for c in BAR_COLUMNS if c not in out.columns]
    if missing:
        raise ValueError(f"bars frame missing columns: {missing}")

    out = out[BAR_COLUMNS]
    ts = pd.to_datetime(out["timestamp"], utc=True)
    out["timestamp"] = ts.dt.tz_convert("UTC").dt.tz_localize(None)
    for col in ["open", "high", "low", "close", "volume"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out.drop_duplicates(subset="timestamp").sort_values("timestamp")
    return out.reset_index(drop=True)


def regular_hours_mask(ts_utc_naive: pd.Series, timeframe: str) -> pd.Series:
    """True for bars that START inside the regular NYSE session (09:30-16:00 ET).

    Daily bars are always True. Hourly bars are clock-aligned (the 09:00 bar holds 09:30-10:00).
    """
    if TIMEFRAME_MINUTES.get(timeframe, 15) >= 1440:
        return pd.Series(True, index=ts_utc_naive.index)
    et = pd.to_datetime(ts_utc_naive).dt.tz_localize("UTC").dt.tz_convert("America/New_York")
    mins = et.dt.hour * 60 + et.dt.minute
    start = 9 * 60 if TIMEFRAME_MINUTES.get(timeframe, 15) >= 60 else 9 * 60 + 30
    return (mins >= start) & (mins < 16 * 60)


def effective_end(end: datetime | None, delay_minutes: int, now: datetime | None = None) -> datetime:
    """Latest instant we may ask a provider for: `end` (default now), but never later than now - delay.

    The free SIP plan refuses/omits the most recent 15 minutes, so requests are clamped to the data horizon.
    """
    now = now or datetime.now(timezone.utc)
    limit = now - timedelta(minutes=max(delay_minutes, 0))
    if end is None:
        return limit
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return min(end, limit)
