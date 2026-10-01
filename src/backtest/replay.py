"""
Offline replay: push synthetic (or stored) bars through the REAL live path (TradingCycle -> decision
engine -> SimBroker -> trade log) bar by bar, with a simulated clock. Produces a demo database so the
dashboard can be previewed without API keys or market hours.

    python -m src.backtest.replay                       # -> data/demo.db (3 symbols, ~8 trading days)
    python -m src.backtest.replay --days 15 --symbols SPY,NVDA --db sqlite:///data/demo.db
    DATABASE_URL=sqlite:///data/demo.db streamlit run src/dashboard/app.py

NOT a performance claim: synthetic data has no real edge. It exists to exercise the plumbing.
"""
from __future__ import annotations

import argparse
import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import src.execution.trade_log as trade_log
import src.scheduler.run_loop as run_loop
from src.config import PROJECT_ROOT, get_settings
from src.data_ingestion.backfill import save_bars
from src.data_ingestion.synthetic import make_bars
from src.db.schema import get_engine, init_db
from src.execution.sim_broker import SimBroker
from src.scheduler.market_hours import to_local


def replay(db_url: str, symbols: list[str], days: int = 8, history_days: int = 40, seed: int = 3,
           equity: float = 100_000.0, settings=None, quiet: bool = True) -> dict:
    s = dataclasses.replace(settings or get_settings(), tickers=symbols)
    path = Path(db_url.replace("sqlite:///", "", 1))
    if path.exists():
        path.unlink()  # a demo DB is always rebuilt from scratch
    engine = get_engine(db_url)
    init_db(engine)

    n_days = history_days + days
    start = pd.bdate_range(end=pd.Timestamp.now().normalize() - pd.Timedelta(days=1), periods=n_days)[0]
    frames = {sym: make_bars(n_days=n_days, start=str(start.date()), seed=seed + 8 * i, start_price=50.0 + 120 * i)
              for i, sym in enumerate(symbols)}
    clock = {"now": None}

    def fetch(sym, tf, a, b):  # simulated feed: only bars that have started by the simulated 'now'
        df = frames[sym]
        a = pd.Timestamp(a).tz_convert("UTC").tz_localize(None)
        return df[(df["timestamp"] >= a) & (df["timestamp"] <= clock["now"])].reset_index(drop=True)

    rng = np.random.default_rng(seed)
    broker = SimBroker(equity=equity)
    msgs: list = []
    cycle = run_loop.TradingCycle(
        engine=engine, broker=broker, settings=s, fetch=fetch,
        notify=lambda m, level="info", engine=None, post=True: msgs.append((level, m)),
        sentiment_fn=lambda sym: dict(score=float(np.clip(rng.normal(0, 0.3), -1, 1)), n=3),
        state_path=path.with_suffix(".state.json"),
    )
    # simulated time for trade timestamps; no trained model in the demo
    sim = {"t": None}
    old_utcnow, old_bundle, old_refresh = trade_log._utcnow, run_loop.load_bundle, run_loop.refresh_sentiment
    trade_log._utcnow = lambda: sim["t"]
    run_loop.load_bundle = lambda: None
    run_loop.refresh_sentiment = lambda *a, **k: None  # no network in a replay
    try:
        ref = frames[symbols[0]]
        ts_all = ref["timestamp"]
        last_day = ts_all.dt.date
        first_replay_day = sorted(set(last_day))[-days]
        idx = [i for i in range(len(ref)) if last_day.iat[i] >= first_replay_day]
        # preload history so indicators are warm
        clock["now"] = ts_all.iat[idx[0] - 1]
        for sym in symbols:
            save_bars(engine, sym, s.timeframe, fetch(sym, s.timeframe, datetime(2000, 1, 1, tzinfo=timezone.utc), None))
        stats = dict(cycles=0, trades=0)
        for i in idx:
            ts = ts_all.iat[i]
            bar_close = (ts + timedelta(minutes=cycle.tf_min, seconds=30)).to_pydatetime()
            sim["t"] = bar_close
            clock["now"] = ts
            for sym in symbols:
                r = frames[sym].iloc[i]
                broker.on_bar(sym, float(r["high"]), float(r["low"]), float(r["close"]))
            is_last_bar_of_day = i + 1 >= len(ref) or last_day.iat[i + 1] != last_day.iat[i]
            if i == idx[0] or last_day.iat[i] != last_day.iat[i - 1]:
                broker.start_new_day()
                cycle.start_session(ts.to_pydatetime())
            if is_last_bar_of_day:
                cycle.maybe_flatten((ts + timedelta(minutes=11)).to_pydatetime())  # 5 min before close
                sim["t"] = bar_close
            out = cycle.run_cycle(now=bar_close)
            stats["cycles"] += 1
            stats["trades"] += out.get("trades", 0)
            if is_last_bar_of_day:
                cycle.end_session(bar_close)
    finally:
        trade_log._utcnow, run_loop.load_bundle, run_loop.refresh_sentiment = old_utcnow, old_bundle, old_refresh
    stats["equity"] = broker.get_account().equity
    stats["db"] = db_url
    stats["errors"] = [m for lvl, m in msgs if lvl == "error"][:5]
    return stats


def main() -> None:
    s = get_settings()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=f"sqlite:///{PROJECT_ROOT / 'data' / 'demo.db'}")
    p.add_argument("--symbols", default="SPY,QQQ,NVDA")
    p.add_argument("--days", type=int, default=8)
    p.add_argument("--seed", type=int, default=3)
    a = p.parse_args()
    out = replay(a.db, [x.strip().upper() for x in a.symbols.split(",") if x.strip()], a.days, seed=a.seed)
    print(out)
    print(f"\nPreview:  DATABASE_URL={a.db} streamlit run src/dashboard/app.py")


if __name__ == "__main__":
    main()
