"""
Decision engine: SMC triple-confirmation signal -> ML probability -> sentiment gate -> hard risk rules.

`should_trade()` is a pure function: given the latest context, account state and settings it
returns a Decision with the full reasoning trail, so every trade (and every skipped signal) can
be explained after the fact. It never talks to a broker.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from src.config import Settings, get_settings
from src.data_ingestion.common import TIMEFRAME_MINUTES
from src.ml.features import row_features
from src.ml.predict import predict_probability
from src.smc_logic.levels import stop_target_levels
from src.smc_logic.pipeline import FEATURE_COLS


@dataclass
class AccountState:
    equity: float
    buying_power: float
    day_start_equity: float
    open_positions: dict[str, float] = field(default_factory=dict)  # symbol -> signed qty

    @property
    def daily_pnl_pct(self) -> float:
        return (self.equity - self.day_start_equity) / self.day_start_equity if self.day_start_equity else 0.0


@dataclass
class Decision:
    symbol: str
    trade: bool = False
    direction: str | None = None
    qty: int = 0
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    probability: float | None = None
    sentiment_score: float | None = None
    blocked_by: str | None = None
    reasons: list[str] = field(default_factory=list)
    signal_time: datetime | None = None
    stop_source: str | None = None
    target_source: str | None = None
    reward_risk: float | None = None
    features: dict = field(default_factory=dict)

    def block(self, code: str, why: str) -> "Decision":
        self.trade, self.blocked_by = False, code
        self.reasons.append(f"BLOCKED[{code}]: {why}")
        return self

    def explain(self) -> str:
        head = (
            f"{self.symbol} {self.direction or '-'} -> "
            + (f"TRADE {self.qty} sh" if self.trade else f"NO TRADE ({self.blocked_by or 'no signal'})")
        )
        return head + "\n  " + "\n  ".join(self.reasons)


def _signal_summary(row: pd.Series, direction: str) -> str:
    return (
        f"signal {direction}: EMA{9}/{21} {'bull' if row['trend_bull'] else 'bear'}, "
        f"RSI {row['rsi']:.1f}, volume {row['vol_ratio']:.2f}x avg"
    )


def pine_exit_reason(row: pd.Series, direction: str, cfg=None) -> str | None:
    """Why the Pine strategy would close an open position on this (closed) bar, else None.

    Mirrors `exitLong = trendBearish or rsi >= 70` / `exitShort = trendBullish or rsi <= 30`.
    """
    hi = getattr(cfg, "rsi_overbought", 70)
    lo = getattr(cfg, "rsi_oversold", 30)
    rsi = float(row["rsi"])
    if direction == "long":
        if bool(row["trend_bear"]):
            return "trend flip (EMA9 < EMA21)"
        if rsi >= hi:
            return f"RSI {rsi:.1f} >= {hi:g}"
    else:
        if bool(row["trend_bull"]):
            return "trend flip (EMA9 > EMA21)"
        if rsi <= lo:
            return f"RSI {rsi:.1f} <= {lo:g}"
    return None


def should_trade(
    symbol: str,
    ctx: pd.DataFrame,
    account: AccountState,
    settings: Settings | None = None,
    bundle: dict | None = None,
    sentiment: dict | None = None,
    now: datetime | None = None,
    timeframe: str | None = None,
) -> Decision:
    """Evaluate the LAST row of `ctx` (the most recently closed bar) for a trade."""
    s = settings or get_settings()
    d = Decision(symbol=symbol)

    if s.kill_switch_active:
        return d.block("kill_switch", "KILL_SWITCH file present")
    if ctx is None or len(ctx) == 0:
        return d.block("no_data", "no bars")

    row = ctx.iloc[-1]
    d.signal_time = pd.Timestamp(row["timestamp"]).to_pydatetime()
    direction = row["signal_event"]
    if direction not in ("long", "short"):
        d.reasons.append("no fresh triple-confirmation signal on the latest closed bar")
        return d
    d.direction = direction
    d.reasons.append(_signal_summary(row, direction))

    # --- data freshness -------------------------------------------------------
    tf_min = TIMEFRAME_MINUTES.get(timeframe or s.timeframe, 15)
    if now is not None:
        bar_close = d.signal_time + timedelta(minutes=tf_min)
        age_bars = (now - bar_close).total_seconds() / 60.0 / tf_min
        if age_bars > s.signal_max_age_bars:
            return d.block("stale_signal", f"signal bar closed {age_bars:.1f} bars ago (max {s.signal_max_age_bars})")

    # --- hard risk rules (independent of the model) ---------------------------
    if direction == "short" and not s.allow_shorts:
        return d.block("shorts_disabled", "ALLOW_SHORTS=false")
    if account.daily_pnl_pct <= -s.max_daily_loss_pct:
        return d.block("daily_loss_halt", f"day P&L {account.daily_pnl_pct:.2%} <= -{s.max_daily_loss_pct:.2%}")
    if len([q for q in account.open_positions.values() if q]) >= s.max_open_positions:
        return d.block("max_positions", f"{len(account.open_positions)} open >= {s.max_open_positions}")
    if account.open_positions.get(symbol):
        return d.block("already_in_position", f"already holding {symbol}")

    # --- ML gate ---------------------------------------------------------------
    sent_score = None
    if sentiment and sentiment.get("n", 0) > 0:
        sent_score = float(sentiment["score"])
    d.sentiment_score = sent_score
    details = {c: row[c] for c in FEATURE_COLS}
    details["zone"] = row["zone"]
    feats = row_features(details, direction, d.signal_time, sentiment)
    d.features = feats
    if bundle is not None:
        d.probability = predict_probability(bundle, feats)
        thr = max(s.min_model_probability, 0.0)
        if d.probability < thr:
            return d.block("model", f"P(win)={d.probability:.2f} < {thr:.2f} ({bundle.get('version')})")
        d.reasons.append(f"model {bundle.get('version')}: P(win)={d.probability:.2f} >= {thr:.2f}")
    elif s.require_model:
        return d.block("no_model", "REQUIRE_MODEL=true and no trained model found")
    else:
        d.reasons.append("no trained model yet: trading rule-only (SMC risk rules still apply)")

    # --- sentiment gate ---------------------------------------------------------
    if sent_score is not None:
        thr = s.sentiment_block_threshold
        if (direction == "long" and sent_score < -thr) or (direction == "short" and sent_score > thr):
            return d.block("sentiment", f"news sentiment {sent_score:+.2f} opposes a {direction}")
        d.reasons.append(f"sentiment {sent_score:+.2f} ({sentiment['n']} items) does not oppose the {direction}")

    # --- stop / target from SMC zones ------------------------------------------
    entry = float(row["close"])
    lv = stop_target_levels(row, direction, entry=entry, min_rr=s.min_rr)
    d.entry, d.stop_loss, d.take_profit = entry, round(lv.stop, 2), round(lv.target, 2)
    pine_exits = s.exit_mode == "pine"
    if pine_exits:
        d.take_profit = None  # the Pine rules close the trade; only the protective stop is placed
    d.stop_source, d.target_source, d.reward_risk = lv.stop_source, lv.target_source, lv.reward_risk
    risk = abs(entry - d.stop_loss)
    atr = float(row["atr"]) if not math.isnan(float(row["atr"])) else entry * 0.005
    if risk <= 0:
        return d.block("bad_levels", "stop on the wrong side of entry")
    if risk / entry > s.max_stop_pct:
        return d.block("stop_too_wide", f"stop {risk / entry:.2%} of price > {s.max_stop_pct:.2%}")
    if risk < s.min_stop_atr * atr:
        return d.block("stop_too_tight", f"stop {risk:.2f} < {s.min_stop_atr} ATR ({atr:.2f}); noise would hit it")
    if pine_exits:
        d.reasons.append(f"protective stop {d.stop_loss} ({lv.stop_source}); exit by Pine rules (RSI 70/30 or trend flip)")
    else:
        d.reasons.append(
            f"stop {d.stop_loss} ({lv.stop_source}), target {d.take_profit} ({lv.target_source}), R:R {lv.reward_risk:.2f}"
        )

    # --- sizing ----------------------------------------------------------------
    risk_dollars = account.equity * s.risk_per_trade_pct
    qty_by_risk = math.floor(risk_dollars / risk)
    qty_by_cap = math.floor(account.equity * s.max_position_pct / entry)
    qty_by_bp = math.floor(account.buying_power / entry)
    qty = min(qty_by_risk, qty_by_cap, qty_by_bp)
    if qty < 1:
        return d.block("size_too_small", f"risk {qty_by_risk}, cap {qty_by_cap}, buying power {qty_by_bp} shares")
    d.qty = qty
    d.trade = True
    d.reasons.append(
        f"size {qty} sh = min(risk {qty_by_risk}, {s.max_position_pct:.0%} equity cap {qty_by_cap}, buying power {qty_by_bp})"
    )
    return d
