"""Pure data/metric functions behind the dashboard (no Streamlit imports -> unit-testable).

All DB timestamps are naive UTC; `to_local()` converts for display.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import select

from src.db.schema import ModelPrediction, Signal, SystemEvent, Trade, session_scope

TZ = "America/Chicago"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series).dt.tz_localize("UTC").dt.tz_convert(TZ)


# ------------------------------------------------------------------ loaders
TRADE_COLS = [
    "id", "symbol", "entry_time", "exit_time", "direction", "entry_price", "exit_price", "qty", "pnl",
    "signal_id", "model_probability", "sentiment_at_entry", "stop_loss", "take_profit", "mode", "status", "note",
]


def load_trades(engine) -> pd.DataFrame:
    with session_scope(engine) as s:
        rows = [{c: getattr(t, c) for c in TRADE_COLS} for t in s.execute(select(Trade).order_by(Trade.id)).scalars()]
        sig = {x.id: (x.signal_type, x.confirmation_details_json or {}) for x in s.execute(select(Signal)).scalars()}
    df = pd.DataFrame(rows, columns=TRADE_COLS)
    for c in ("entry_time", "exit_time"):
        df[c] = pd.to_datetime(df[c])
    df["signal_type"] = df["signal_id"].map(lambda i: sig.get(i, ("manual/unknown", {}))[0] if pd.notna(i) else "manual/unknown")
    df["zone"] = df["signal_id"].map(lambda i: sig.get(i, (None, {}))[1].get("zone") if pd.notna(i) else None)
    return df


def closed(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    return trades[(trades["status"] == "closed") & trades["pnl"].notna()].sort_values("exit_time").reset_index(drop=True)


def load_events(engine, hours: int = 24 * 7, kinds: list[str] | None = None) -> pd.DataFrame:
    since = utcnow() - timedelta(hours=hours)
    with session_scope(engine) as s:
        q = select(SystemEvent).where(SystemEvent.timestamp >= since).order_by(SystemEvent.timestamp.desc())
        rows = [dict(timestamp=e.timestamp, kind=e.kind, message=e.message) for e in s.execute(q).scalars()]
    df = pd.DataFrame(rows, columns=["timestamp", "kind", "message"])
    if kinds:
        df = df[df["kind"].isin(kinds)]
    return df


def latest_model_probability(engine) -> tuple[float | None, str | None, datetime | None]:
    with session_scope(engine) as s:
        p = s.execute(select(ModelPrediction).order_by(ModelPrediction.timestamp.desc()).limit(1)).scalars().first()
    return (p.probability, p.model_version, p.timestamp) if p else (None, None, None)


# ------------------------------------------------------------------ metrics
def max_drawdown(equity: pd.Series) -> float:
    """Largest peak-to-trough decline as a negative fraction (0 if none)."""
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float(((equity - peak) / peak).min())


def sharpe_ratio(daily_returns: pd.Series, periods: int = 252) -> float | None:
    r = daily_returns.dropna()
    if len(r) < 3 or r.std(ddof=1) == 0:
        return None
    return float(r.mean() / r.std(ddof=1) * np.sqrt(periods))


def equity_curve(trades: pd.DataFrame, start_equity: float = 100_000.0) -> pd.DataFrame:
    """Equity after each closed trade (realised P&L only), with the trade mode for paper/live shading."""
    c = closed(trades)
    if c.empty:
        return pd.DataFrame(columns=["time", "equity", "mode", "pnl"])
    eq = start_equity + c["pnl"].cumsum()
    return pd.DataFrame({"time": c["exit_time"], "equity": eq, "mode": c["mode"], "pnl": c["pnl"]})


def daily_pnl(trades: pd.DataFrame) -> pd.Series:
    c = closed(trades)
    if c.empty:
        return pd.Series(dtype=float)
    day = to_local(c["exit_time"]).dt.date
    return c.groupby(day.values)["pnl"].sum()


def rolling_sharpe(trades: pd.DataFrame, start_equity: float, window: int = 20) -> pd.Series:
    d = daily_pnl(trades)
    if d.empty:
        return pd.Series(dtype=float)
    equity_prev = start_equity + d.cumsum().shift(1).fillna(0)
    ret = d / equity_prev
    return ret.rolling(window, min_periods=min(window, 5)).apply(lambda x: sharpe_ratio(pd.Series(x)) or np.nan, raw=False)


def _period_pnl(c: pd.DataFrame, since_local: pd.Timestamp) -> float:
    if c.empty:
        return 0.0
    t = to_local(c["exit_time"])
    return float(c.loc[t >= since_local, "pnl"].sum())


def kpis(trades: pd.DataFrame, start_equity: float = 100_000.0, now: datetime | None = None,
         open_exposure: float | None = None, model_prob: float | None = None) -> dict:
    now = now or utcnow()
    c = closed(trades)
    local_now = pd.Timestamp(now, tz="UTC").tz_convert(TZ)
    today0 = local_now.normalize()
    week0 = today0 - pd.Timedelta(days=today0.weekday())
    month0 = today0.replace(day=1)
    wins = c[c["pnl"] > 0] if not c.empty else c
    losses = c[c["pnl"] < 0] if not c.empty else c
    avg_win = float(wins["pnl"].mean()) if len(wins) else 0.0
    avg_loss = float(-losses["pnl"].mean()) if len(losses) else 0.0
    eq = equity_curve(trades, start_equity)
    sharpe = sharpe_ratio((daily_pnl(trades) / (start_equity + daily_pnl(trades).cumsum().shift(1).fillna(0)))) if not c.empty else None
    if open_exposure is None and not trades.empty:
        o = trades[trades["status"].isin(["open", "filled"])]
        open_exposure = float((o["qty"] * o["entry_price"].fillna(0)).sum())
    return dict(
        pnl_today=_period_pnl(c, today0), pnl_week=_period_pnl(c, week0), pnl_month=_period_pnl(c, month0),
        pnl_all=float(c["pnl"].sum()) if not c.empty else 0.0,
        trades=int(len(c)), wins=int(len(wins)), losses=int(len(losses)),
        win_rate=(len(wins) / len(c)) if len(c) else None,
        win_loss_ratio=(avg_win / avg_loss) if avg_loss > 0 else None,
        avg_win=avg_win, avg_loss=avg_loss,
        profit_factor=(float(wins["pnl"].sum() / -losses["pnl"].sum()) if len(losses) and losses["pnl"].sum() < 0 else None),
        open_exposure=open_exposure or 0.0,
        sharpe=sharpe, max_drawdown=max_drawdown(eq["equity"]) if not eq.empty else 0.0,
        model_prob=model_prob,
    )


def _wr(g: pd.DataFrame) -> pd.Series:
    return pd.Series({"trades": len(g), "win_rate": (g["pnl"] > 0).mean(), "pnl": g["pnl"].sum()})


def winrate_by_signal(trades: pd.DataFrame) -> pd.DataFrame:
    c = closed(trades)
    if c.empty:
        return pd.DataFrame(columns=["group", "trades", "win_rate", "pnl"])
    c = c.assign(group=c["signal_type"].fillna("unknown") + " / " + c["direction"])
    return c.groupby("group").apply(_wr, include_groups=False).reset_index()


SENTIMENT_BUCKETS = [(-1.01, -0.2, "negative (< -0.2)"), (-0.2, 0.2, "neutral"), (0.2, 1.01, "positive (> 0.2)")]


def winrate_by_sentiment(trades: pd.DataFrame) -> pd.DataFrame:
    c = closed(trades)
    c = c[c["sentiment_at_entry"].notna()] if not c.empty else c
    if c.empty:
        return pd.DataFrame(columns=["group", "trades", "win_rate", "pnl"])
    bins = [b[0] for b in SENTIMENT_BUCKETS] + [SENTIMENT_BUCKETS[-1][1]]
    labels = [b[2] for b in SENTIMENT_BUCKETS]
    c = c.assign(group=pd.cut(c["sentiment_at_entry"], bins=bins, labels=labels))
    out = c.groupby("group", observed=True).apply(_wr, include_groups=False).reset_index()
    out["group"] = out["group"].astype(str)
    return out


def health(engine, bars_latest: datetime | None, now: datetime | None = None, trading_mode: str = "paper") -> dict:
    """Last/next run, API-error count (24h), data staleness, mode — for the System Health panel."""
    now = now or utcnow()
    ev = load_events(engine, hours=24)
    cycles = ev[ev["kind"] == "cycle"]
    errors = ev[ev["kind"] == "error"]
    last_cycle = cycles["timestamp"].max() if not cycles.empty else None
    stale_min = (now - bars_latest).total_seconds() / 60 if bars_latest is not None else None
    heartbeat_min = (now - last_cycle).total_seconds() / 60 if last_cycle is not None else None
    try:
        from src.scheduler.market_hours import is_trading_window_now, next_session_open

        in_window = is_trading_window_now(now)
        nxt = None if in_window else next_session_open(now)
    except Exception:  # noqa: BLE001
        in_window, nxt = None, None
    return dict(
        mode=trading_mode, last_cycle=last_cycle, minutes_since_cycle=heartbeat_min, errors_24h=int(len(errors)),
        bars_latest=bars_latest, data_stale_minutes=stale_min, in_trading_window=in_window, next_open=nxt,
        # healthy = ran recently (when market is open) and no error burst
        status=("ok" if (errors.shape[0] < 5 and (not in_window or (heartbeat_min is not None and heartbeat_min < 40))) else "attention"),
    )
