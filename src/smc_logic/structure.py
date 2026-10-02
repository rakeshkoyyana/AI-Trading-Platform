"""
Market structure: legs/pivots, BOS/CHoCH, EQH/EQL, trailing extremes.

Faithful, stateful, bar-by-bar port of the LuxAlgo-style Smart Money Concepts
Pine script. All outputs at bar *i* depend only on bars <= i (no look-ahead);
pivots are confirmed `size` bars after they occur, exactly as in Pine.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.smc_logic.config import SMCConfig
from src.smc_logic.indicators import atr as _atr
from src.smc_logic.indicators import true_range

BULLISH, BEARISH = 1, -1


@dataclass
class _Pivot:
    level: float = np.nan
    last: float = np.nan
    crossed: bool = False
    bar: int = 0


@dataclass
class StructureResult:
    frame: pd.DataFrame
    events: list[dict] = field(default_factory=list)  # one dict per BOS/CHoCH


def _crossover(c: float, c1: float, lvl: float, lvl1: float) -> bool:
    if np.isnan(c) or np.isnan(c1) or np.isnan(lvl) or np.isnan(lvl1):
        return False
    return c > lvl and c1 <= lvl1


def _crossunder(c: float, c1: float, lvl: float, lvl1: float) -> bool:
    if np.isnan(c) or np.isnan(c1) or np.isnan(lvl) or np.isnan(lvl1):
        return False
    return c < lvl and c1 >= lvl1


def detect_structure(df: pd.DataFrame, cfg: SMCConfig | None = None) -> StructureResult:
    """Run the full structure state machine over an OHLCV frame.

    Returns a frame aligned to `df` with (among others):
      swing_trend, internal_trend            -> +1 / -1 / 0 bias after the bar
      {swing,int}_{bull,bear}_{bos,choch}    -> bool event flags on that bar
      eqh, eql                               -> equal-high / equal-low confirmed this bar
      swing_high, swing_low, int_high, int_low -> current pivot levels (NaN until first pivot)
      trail_top, trail_bottom                -> trailing swing extremes
      atr200, parsed_high, parsed_low        -> volatility measure + OB-filtered extremes
    """
    cfg = cfg or SMCConfig()
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    n = len(df)

    tr = true_range(h, l, c)
    atr200 = _atr(h, l, c, 200)
    if cfg.ob_filter == "atr":
        vol_measure = atr200
    else:  # cumulative mean range: ta.cum(ta.tr) / bar_index
        with np.errstate(divide="ignore", invalid="ignore"):
            vol_measure = np.cumsum(tr) / np.arange(n, dtype=float)
    with np.errstate(invalid="ignore"):
        high_vol = (h - l) >= 2.0 * vol_measure  # NaN compares False, like Pine na
    parsed_high = np.where(high_vol, l, h)
    parsed_low = np.where(high_vol, h, l)

    sizes = {"swing": cfg.swing_length, "internal": cfg.internal_length, "equal": cfg.equal_length}
    leg = {k: 0 for k in sizes}
    prev_leg: dict[str, int | None] = {k: None for k in sizes}

    swing_high, swing_low = _Pivot(), _Pivot()
    int_high, int_low = _Pivot(), _Pivot()
    eq_high, eq_low = _Pivot(), _Pivot()
    swing_bias = 0
    internal_bias = 0
    trail_top = trail_bottom = np.nan
    trail_top_idx = trail_bottom_idx = 0

    names = [
        "swing_bull_bos", "swing_bull_choch", "swing_bear_bos", "swing_bear_choch",
        "int_bull_bos", "int_bull_choch", "int_bear_bos", "int_bear_choch",
        "eqh", "eql",
    ]
    flags = {k: np.zeros(n, dtype=bool) for k in names}
    out = {
        k: np.full(n, np.nan)
        for k in [
            "swing_high", "swing_low", "int_high", "int_low",
            "trail_top", "trail_bottom", "trail_top_idx", "trail_bottom_idx",
        ]
    }
    swing_trend = np.zeros(n, dtype=int)
    internal_trend = np.zeros(n, dtype=int)
    events: list[dict] = []

    # previous-bar snapshots of the pivot-level series fed to ta.crossover/crossunder
    prev = {"swing_high": np.nan, "swing_low": np.nan, "int_high": np.nan, "int_low": np.nan}

    for i in range(n):
        # 1) trailing extremes (Pine math.max/min return na if either operand is na)
        if not np.isnan(trail_top):
            trail_top = max(h[i], trail_top)
            if trail_top == h[i]:
                trail_top_idx = i
        if not np.isnan(trail_bottom):
            trail_bottom = min(l[i], trail_bottom)
            if trail_bottom == l[i]:
                trail_bottom_idx = i

        # 2) legs and pivots
        for kind, size in sizes.items():
            if i >= size:
                new_leg_high = h[i - size] > np.max(h[i - size + 1 : i + 1])
                new_leg_low = l[i - size] < np.min(l[i - size + 1 : i + 1])
                if new_leg_high:
                    leg[kind] = 0
                elif new_leg_low:
                    leg[kind] = 1
            change = None if prev_leg[kind] is None else leg[kind] - prev_leg[kind]
            prev_leg[kind] = leg[kind]
            if not change:  # None (first bar) or 0
                continue

            if change == +1:  # pivot LOW confirmed at low[size]
                piv = eq_low if kind == "equal" else int_low if kind == "internal" else swing_low
                lvl = l[i - size]
                if kind == "equal":
                    if abs(piv.level - lvl) < cfg.equal_threshold * atr200[i]:
                        flags["eql"][i] = True
                piv.last, piv.level, piv.crossed, piv.bar = piv.level, lvl, False, i - size
                if kind == "swing":
                    trail_bottom, trail_bottom_idx = lvl, i - size
            else:  # change == -1: pivot HIGH confirmed at high[size]
                piv = eq_high if kind == "equal" else int_high if kind == "internal" else swing_high
                lvl = h[i - size]
                if kind == "equal":
                    if abs(piv.level - lvl) < cfg.equal_threshold * atr200[i]:
                        flags["eqh"][i] = True
                piv.last, piv.level, piv.crossed, piv.bar = piv.level, lvl, False, i - size
                if kind == "swing":
                    trail_top, trail_top_idx = lvl, i - size

        # 3) structure breaks: internal first, then swing (matches Pine call order)
        for internal in (True, False):
            p_hi = int_high if internal else swing_high
            p_lo = int_low if internal else swing_low
            pre = "int" if internal else "swing"
            bias = internal_bias if internal else swing_bias

            bullish_bar = bearish_bar = True
            if cfg.internal_confluence_filter:
                bullish_bar = (h[i] - max(c[i], o[i])) > min(c[i], o[i] - l[i])
                bearish_bar = (h[i] - max(c[i], o[i])) < min(c[i], o[i] - l[i])

            extra = (int_high.level != swing_high.level and bullish_bar) if internal else True
            c1 = c[i - 1] if i > 0 else np.nan
            if _crossover(c[i], c1, p_hi.level, prev[f"{pre}_high"]) and not p_hi.crossed and extra:
                tag = "choch" if bias == BEARISH else "bos"
                flags[f"{pre}_bull_{tag}"][i] = True
                events.append(
                    dict(i=i, internal=internal, bias=BULLISH, tag=tag, pivot_bar=p_hi.bar, level=p_hi.level)
                )
                p_hi.crossed = True
                bias = BULLISH

            extra = (int_low.level != swing_low.level and bearish_bar) if internal else True
            if _crossunder(c[i], c1, p_lo.level, prev[f"{pre}_low"]) and not p_lo.crossed and extra:
                tag = "choch" if bias == BULLISH else "bos"
                flags[f"{pre}_bear_{tag}"][i] = True
                events.append(
                    dict(i=i, internal=internal, bias=BEARISH, tag=tag, pivot_bar=p_lo.bar, level=p_lo.level)
                )
                p_lo.crossed = True
                bias = BEARISH

            if internal:
                internal_bias = bias
            else:
                swing_bias = bias

        # end-of-bar snapshots
        prev.update(
            swing_high=swing_high.level, swing_low=swing_low.level,
            int_high=int_high.level, int_low=int_low.level,
        )
        out["swing_high"][i], out["swing_low"][i] = swing_high.level, swing_low.level
        out["int_high"][i], out["int_low"][i] = int_high.level, int_low.level
        out["trail_top"][i], out["trail_bottom"][i] = trail_top, trail_bottom
        out["trail_top_idx"][i], out["trail_bottom_idx"][i] = trail_top_idx, trail_bottom_idx
        swing_trend[i], internal_trend[i] = swing_bias, internal_bias

    frame = pd.DataFrame(
        {
            **{k: v for k, v in flags.items()},
            **out,
            "swing_trend": swing_trend,
            "internal_trend": internal_trend,
            "atr200": atr200,
            "parsed_high": parsed_high,
            "parsed_low": parsed_low,
        },
        index=df.index,
    )
    return StructureResult(frame=frame, events=events)


def detect_bos(df: pd.DataFrame, cfg: SMCConfig | None = None) -> pd.DataFrame:
    """Boolean BOS columns (swing + internal, bull + bear)."""
    f = detect_structure(df, cfg).frame
    return f[[c for c in f.columns if c.endswith("_bos")]]


def detect_choch(df: pd.DataFrame, cfg: SMCConfig | None = None) -> pd.DataFrame:
    """Boolean CHoCH columns (swing + internal, bull + bear)."""
    f = detect_structure(df, cfg).frame
    return f[[c for c in f.columns if c.endswith("_choch")]]
