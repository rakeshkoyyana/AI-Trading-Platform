"""
Historical backfill: pulls bars for every target ticker into the `bars` table.

Usage:
    python -m src.data_ingestion.backfill                       # 12 months, settings tickers
    python -m src.data_ingestion.backfill --months 6 --symbols SPY,ASTS
    python -m src.data_ingestion.backfill --incremental         # only bars after the latest stored
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.config import get_settings
from src.data_ingestion import get_bars_with_fallback
from src.db.schema import Bar, get_engine, init_db, session_scope


def latest_bar_time(engine, symbol: str, timeframe: str) -> datetime | None:
    with session_scope(engine) as s:
        return s.execute(
            select(func.max(Bar.timestamp)).where(
                Bar.symbol == symbol, Bar.timeframe == timeframe
            )
        ).scalar()


def save_bars(engine, symbol: str, timeframe: str, df: pd.DataFrame) -> int:
    """Insert bars, ignoring ones already stored. Returns rows offered for insert."""
    if df.empty:
        return 0
    rows = [
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": r.timestamp.to_pydatetime(),
            "open": float(r.open),
            "high": float(r.high),
            "low": float(r.low),
            "close": float(r.close),
            "volume": float(r.volume),
        }
        for r in df.itertuples(index=False)
    ]
    with session_scope(engine) as s:
        for i in range(0, len(rows), 5000):
            chunk = rows[i : i + 5000]
            if engine.dialect.name == "sqlite":
                s.execute(sqlite_insert(Bar).on_conflict_do_nothing(), chunk)
            else:  # generic path for Postgres/Supabase etc.
                s.bulk_insert_mappings(Bar, chunk)
    return len(rows)


def load_bars(engine, symbol: str, timeframe: str, since: datetime | None = None) -> pd.DataFrame:
    q = select(Bar).where(Bar.symbol == symbol, Bar.timeframe == timeframe)
    if since is not None:
        q = q.where(Bar.timestamp >= since)
    q = q.order_by(Bar.timestamp)
    with session_scope(engine) as s:
        rows = s.execute(q).scalars().all()
    return pd.DataFrame(
        [
            {
                "timestamp": b.timestamp,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in rows
        ],
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )


def backfill(
    symbols: list[str],
    timeframe: str,
    months: int = 12,
    incremental: bool = False,
    engine=None,
    fetch=get_bars_with_fallback,
) -> dict[str, int]:
    engine = engine or get_engine()
    init_db(engine)
    now = datetime.now(timezone.utc)
    results: dict[str, int] = {}
    for sym in symbols:
        start = now - timedelta(days=30 * months)
        if incremental:
            last = latest_bar_time(engine, sym, timeframe)
            if last is not None:
                start = last.replace(tzinfo=timezone.utc) + timedelta(minutes=1)
        try:
            df = fetch(sym, timeframe, start, now)
        except Exception as exc:  # noqa: BLE001
            print(f"[backfill] {sym}: FAILED ({exc})")
            results[sym] = 0
            continue
        n = save_bars(engine, sym, timeframe, df)
        results[sym] = n
        print(f"[backfill] {sym}: {n} bars ({timeframe}) from {start:%Y-%m-%d}")
    return results


def main() -> None:
    s = get_settings()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", default=",".join(s.tickers))
    p.add_argument("--timeframe", default=s.timeframe)
    p.add_argument("--months", type=int, default=12)
    p.add_argument("--incremental", action="store_true")
    a = p.parse_args()
    backfill(
        [x.strip().upper() for x in a.symbols.split(",") if x.strip()],
        a.timeframe,
        a.months,
        a.incremental,
    )


if __name__ == "__main__":
    main()
