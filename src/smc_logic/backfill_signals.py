"""
Compute signals + labels over stored bars and write them to the `signals` table.

    python -m src.smc_logic.backfill_signals                 # all settings tickers
    python -m src.smc_logic.backfill_signals --symbols SPY --horizon 8
"""
from __future__ import annotations

import argparse
import math

import pandas as pd
from sqlalchemy import select

from src.config import get_settings
from src.data_ingestion.backfill import load_bars
from src.db.schema import ModelPrediction, Signal, Trade, get_engine, init_db, session_scope
from src.smc_logic.label import label_signals
from src.smc_logic.pipeline import FEATURE_COLS, compute_context

SIGNAL_TYPE = "triple_confirmation"


def _clean(v):
    if v is None:
        return None
    if isinstance(v, (bool,)):
        return bool(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return None if math.isnan(f) else (int(f) if float(f).is_integer() and not isinstance(v, float) else f)


def build_signal_rows(ctx: pd.DataFrame, labels: pd.DataFrame) -> list[dict]:
    rows = []
    for lab in labels.itertuples(index=False):
        r = ctx.iloc[lab.bar]
        details = {c: _clean(r[c]) for c in FEATURE_COLS}
        details["zone"] = str(r["zone"])
        details["exit_reason"] = lab.exit_reason
        details["bars_held"] = _clean(lab.bars_held)
        rows.append(
            dict(
                timestamp=lab.timestamp.to_pydatetime(),
                direction=lab.direction,
                entry_price=_clean(lab.entry_price),
                forward_return=_clean(lab.forward_return),
                label_win=None if pd.isna(lab.label_win) else int(lab.label_win),
                confirmation_details_json=details,
            )
        )
    return rows


def upsert_signal_event(engine, symbol: str, timeframe: str, ctx: pd.DataFrame, i: int | None = None) -> int | None:
    """Store one live signal (the event at row `i`, default last row) without labels.

    Labels are filled later by `backfill_signals` once forward bars exist. Returns the signal id,
    or None if that bar carries no signal event.
    """
    i = len(ctx) - 1 if i is None else i
    direction = ctx["signal_event"].iat[i]
    if direction not in ("long", "short"):
        return None
    r = ctx.iloc[i]
    details = {c: _clean(r[c]) for c in FEATURE_COLS}
    details["zone"] = str(r["zone"])
    ts = pd.Timestamp(r["timestamp"]).to_pydatetime()
    with session_scope(engine) as s:
        sg = s.execute(
            select(Signal).where(
                Signal.symbol == symbol, Signal.timeframe == timeframe,
                Signal.signal_type == SIGNAL_TYPE, Signal.timestamp == ts,
            )
        ).scalars().first()
        if sg is None:
            sg = Signal(
                symbol=symbol, timeframe=timeframe, signal_type=SIGNAL_TYPE, timestamp=ts,
                direction=direction, entry_price=_clean(r["close"]), confirmation_details_json=details,
            )
            s.add(sg)
            s.flush()
        return sg.id


def backfill_signals(
    symbols: list[str], timeframe: str, horizon: int = 8, min_return: float = 0.0, engine=None
) -> dict[str, int]:
    engine = engine or get_engine()
    init_db(engine)
    out: dict[str, int] = {}
    for sym in symbols:
        bars = load_bars(engine, sym, timeframe)
        if len(bars) < 250:
            print(f"[signals] {sym}: only {len(bars)} bars, need >=250; skipping")
            out[sym] = 0
            continue
        ctx = compute_context(bars)
        labels = label_signals(ctx, horizon=horizon, min_return=min_return)
        rows = build_signal_rows(ctx, labels)
        with session_scope(engine) as s:
            existing = {
                sg.timestamp: sg
                for sg in s.execute(
                    select(Signal).where(
                        Signal.symbol == sym,
                        Signal.timeframe == timeframe,
                        Signal.signal_type == SIGNAL_TYPE,
                    )
                ).scalars()
            }
            # Signals stored earlier that the current bars no longer produce (e.g. after the data feed changed
            # from IEX to SIP) would poison training; drop them unless a trade or prediction refers to them.
            fresh = {r["timestamp"] for r in rows}
            first_ts = bars["timestamp"].iat[0].to_pydatetime()
            used = set(s.execute(select(Trade.signal_id).where(Trade.signal_id.is_not(None))).scalars()) | set(
                s.execute(select(ModelPrediction.signal_id).where(ModelPrediction.signal_id.is_not(None))).scalars())
            for ts, sg in existing.items():
                if ts >= first_ts and ts not in fresh and sg.id not in used:
                    s.delete(sg)
            for r in rows:
                sg = existing.get(r["timestamp"])
                if sg is None:
                    s.add(Signal(symbol=sym, timeframe=timeframe, signal_type=SIGNAL_TYPE, **r))
                else:  # refresh labels/details as more forward data arrives
                    for k, v in r.items():
                        setattr(sg, k, v)
        out[sym] = len(rows)
        print(f"[signals] {sym}: {len(rows)} signals ({int(labels['label_win'].notna().sum())} labelled)")
    return out


def main() -> None:
    s = get_settings()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", default=",".join(s.tickers))
    p.add_argument("--timeframe", default=s.timeframe)
    p.add_argument("--horizon", type=int, default=8)
    p.add_argument("--min-return", type=float, default=0.0)
    a = p.parse_args()
    backfill_signals(
        [x.strip().upper() for x in a.symbols.split(",") if x.strip()],
        a.timeframe,
        a.horizon,
        a.min_return,
    )


if __name__ == "__main__":
    main()
