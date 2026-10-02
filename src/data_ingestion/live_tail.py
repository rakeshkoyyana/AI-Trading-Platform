"""Free "hybrid" live candles.

The free Alpaca plan serves consolidated SIP bars only once they are ~15 min old, but IEX bars are
real-time. For the newest minutes we take IEX prices (liquid names track the consolidated tape
closely) and rescale IEX volume by k = median(SIP volume / IEX volume) measured on the overlapping
bars, so the volume-spike confirmation stays comparable. Estimated bars are flagged `est=True`,
never written to the database, and replaced by the exact SIP bar once it becomes available.
Calibration quality (price MAPE, volume-spike agreement) is returned so it can be logged.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from src.data_ingestion.common import BAR_COLUMNS, regular_hours_mask


def estimate_tail(sip: pd.DataFrame, iex: pd.DataFrame, timeframe: str = "15Min", min_overlap: int = 8,
                  calib_bars: int = 60) -> tuple[pd.DataFrame, dict]:
    """Bars newer than the last SIP bar, built from IEX. Returns (tail, diagnostics)."""
    diag: dict = dict(k=None, overlap=0, n_tail=0, price_mape=None, spike_agree=None, reason=None)
    empty = pd.DataFrame(columns=[*BAR_COLUMNS, "est"])
    if sip is None or len(sip) == 0 or iex is None or len(iex) == 0:
        diag["reason"] = "no data"
        return empty, diag

    last = sip["timestamp"].max()
    m = sip.merge(iex, on="timestamp", suffixes=("_s", "_i"))
    m = m[(m["volume_i"] > 0) & (m["volume_s"] > 0)]
    m = m[regular_hours_mask(m["timestamp"], timeframe)].tail(calib_bars)  # extended-hours IEX is too sparse
    diag["overlap"] = int(len(m))
    if len(m) < min_overlap:
        diag["reason"] = f"only {len(m)} overlapping regular-hours bars (< {min_overlap})"
        return empty, diag

    k = float(np.clip(np.median(m["volume_s"] / m["volume_i"]), 1.0, 500.0))
    diag["k"] = k
    diag["price_mape"] = float((np.abs(m["close_i"] - m["close_s"]) / m["close_s"]).mean())
    vs, vi = m["volume_s"] > m["volume_s"].rolling(20, min_periods=5).mean() * 1.2, \
        m["volume_i"] * k > (m["volume_s"].rolling(20, min_periods=5).mean() * 1.2)
    diag["spike_agree"] = float((vs == vi).mean())

    tail = iex[iex["timestamp"] > last][BAR_COLUMNS].copy()
    tail["volume"] = tail["volume"] * k
    tail["est"] = True
    diag["n_tail"] = int(len(tail))
    return tail.reset_index(drop=True), diag


def with_live_tail(bars: pd.DataFrame, symbol: str, timeframe: str, fetch_iex=None,
                   now: datetime | None = None, lookback_days: int = 3,
                   regular_only: bool = False) -> tuple[pd.DataFrame, dict]:
    """`bars` (SIP, exact) + estimated recent bars. Falls back to `bars` untouched on any failure."""
    out = bars.copy()
    out["est"] = False
    if bars is None or len(bars) == 0:
        return out, dict(reason="no bars")
    now = now or datetime.now(timezone.utc)
    start = bars["timestamp"].max().to_pydatetime() - timedelta(days=lookback_days)
    try:
        if fetch_iex is None:
            from src.data_ingestion.alpaca_data import get_bars

            def fetch_iex(sym, tf, st, en):  # noqa: E306
                return get_bars(sym, tf, st, en, feed="iex")
        iex = fetch_iex(symbol, timeframe, start.replace(tzinfo=timezone.utc), now)
    except Exception as exc:  # noqa: BLE001 - live tail is best effort
        return out, dict(reason=f"IEX fetch failed: {exc}")
    tail, diag = estimate_tail(bars, iex, timeframe)
    if regular_only and len(tail):  # extended-hours bars were not asked for
        tail = tail[regular_hours_mask(tail["timestamp"], timeframe).to_numpy()]
        diag["n_tail"] = int(len(tail))
    if len(tail):
        out = pd.concat([out, tail], ignore_index=True)
    return out, diag
