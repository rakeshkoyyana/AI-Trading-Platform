"""Load a symbol's bars only when someone opens it (TradingView/Robinhood style), then keep them cached.

The first open of a new symbol backfills history from the configured feed; later opens only top up the
newest bars. Nothing here runs in the background, so an unopened symbol costs nothing.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.data_ingestion import get_bars_with_fallback
from src.data_ingestion.backfill import backfill, latest_bar_time
from src.db.schema import get_engine, init_db

# timeframe -> months of history pulled the first time a symbol is opened
FIRST_LOAD = {"15Min": 12, "5Min": 2}


def ensure_symbol_data(symbol: str, engine=None, fetch=get_bars_with_fallback, first_load: dict | None = None) -> dict:
    """Make sure `symbol` has up-to-date 15Min and 5Min bars. Never raises; returns a small status dict."""
    sym = symbol.strip().upper()
    engine = engine or get_engine()
    init_db(engine)
    out = dict(symbol=sym, new_symbol=False, bars={}, error=None)
    try:
        for tf, months in (first_load or FIRST_LOAD).items():
            first = latest_bar_time(engine, sym, tf) is None
            out["new_symbol"] = out["new_symbol"] or first
            res = backfill([sym], tf, months=months, incremental=not first, engine=engine, fetch=fetch)
            out["bars"][tf] = res.get(sym, 0)
        if out["new_symbol"] and not any(out["bars"].values()):
            out["error"] = f"no data returned for {sym} (check the ticker, or your Alpaca keys)"
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    out["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out
