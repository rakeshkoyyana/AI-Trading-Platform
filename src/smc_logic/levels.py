"""Stop-loss / take-profit anchored on SMC zones, with an ATR fallback."""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Levels:
    entry: float
    stop: float
    target: float
    stop_source: str
    target_source: str

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward_risk(self) -> float:
        return abs(self.target - self.entry) / self.risk if self.risk > 0 else 0.0


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def stop_target_levels(
    row: pd.Series,
    direction: str,
    entry: float | None = None,
    stop_buffer_atr: float = 0.1,
    fallback_stop_atr: float = 1.5,
    min_rr: float = 1.5,
) -> Levels:
    """Stop beyond the nearest protective SMC level; target at the nearest opposing level.

    long : stop below nearest active bullish OB low, else swing low, else ATR stop
           target = nearest opposing bearish OB low / swing high / trailing top above entry
    short: mirrored.
    Target is pushed out to `min_rr` if the structural target is closer than that.
    """
    entry = float(entry if entry is not None else row["close"])
    atr = float(row["atr"]) if _ok(row.get("atr")) else entry * 0.005
    buf = stop_buffer_atr * atr
    long = direction == "long"

    stop, stop_src = None, "atr"
    if long:
        for col, src in (("bull_ob_low", "order_block"), ("swing_low", "swing_low")):
            v = row.get(col)
            if _ok(v) and v < entry:
                stop, stop_src = float(v) - buf, src
                break
        if stop is None:
            stop = entry - fallback_stop_atr * atr
    else:
        for col, src in (("bear_ob_high", "order_block"), ("swing_high", "swing_high")):
            v = row.get(col)
            if _ok(v) and v > entry:
                stop, stop_src = float(v) + buf, src
                break
        if stop is None:
            stop = entry + fallback_stop_atr * atr

    risk = abs(entry - stop)
    target, tgt_src = None, "rr"
    if long:
        for col, src in (("bear_ob_low", "order_block"), ("swing_high", "swing_high"), ("trail_top", "trail_top")):
            v = row.get(col)
            if _ok(v) and v > entry:
                target, tgt_src = float(v), src
                break
    else:
        for col, src in (("bull_ob_high", "order_block"), ("swing_low", "swing_low"), ("trail_bottom", "trail_bottom")):
            v = row.get(col)
            if _ok(v) and v < entry:
                target, tgt_src = float(v), src
                break

    min_target = entry + min_rr * risk if long else entry - min_rr * risk
    if target is None or (long and target < min_target) or (not long and target > min_target):
        target, tgt_src = min_target, "rr"
    return Levels(entry, stop, target, stop_src, tgt_src)
