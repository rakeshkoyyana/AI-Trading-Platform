"""Fair value gaps (chart-timeframe, auto-threshold) — port of drawFairValueGaps()."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.smc_logic.config import SMCConfig


def detect_fvg(df: pd.DataFrame, cfg: SMCConfig | None = None) -> pd.DataFrame:
    """Per-bar FVG events and active-gap context.

    Columns: fvg_bull_new, fvg_bear_new, fvg_bull_active, fvg_bear_active,
             in_bull_fvg, in_bear_fvg (close inside an active gap of that type).
    """
    cfg = cfg or SMCConfig()
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    n = len(df)

    bull_new = np.zeros(n, dtype=bool)
    bear_new = np.zeros(n, dtype=bool)
    bull_cnt = np.zeros(n, dtype=int)
    bear_cnt = np.zeros(n, dtype=int)
    in_bull = np.zeros(n, dtype=bool)
    in_bear = np.zeros(n, dtype=bool)

    gaps: list[dict] = []  # newest first
    cum = 0.0
    for i in range(n):
        # deleteFairValueGaps(): runs before the new gap is drawn on this bar
        gaps = [
            g
            for g in gaps
            if not ((l[i] < g["bottom"] and g["bias"] == 1) or (h[i] > g["top"] and g["bias"] == -1))
        ]

        if i >= 1:
            delta = (c[i - 1] - o[i - 1]) / (o[i - 1] * 100.0)
            cum += abs(delta)
        else:
            delta = np.nan
        if i >= 2:
            threshold = (cum / i * 2.0) if cfg.fvg_auto_threshold else 0.0
            last_close, last2_high, last2_low = c[i - 1], h[i - 2], l[i - 2]
            if l[i] > last2_high and last_close > last2_high and delta > threshold:
                bull_new[i] = True
                gaps.insert(0, dict(top=l[i], bottom=last2_high, bias=1))
            if h[i] < last2_low and last_close < last2_low and -delta > threshold:
                bear_new[i] = True
                gaps.insert(0, dict(top=h[i], bottom=last2_low, bias=-1))

        bull_cnt[i] = sum(1 for g in gaps if g["bias"] == 1)
        bear_cnt[i] = sum(1 for g in gaps if g["bias"] == -1)
        in_bull[i] = any(g["bias"] == 1 and g["bottom"] <= c[i] <= g["top"] for g in gaps)
        in_bear[i] = any(g["bias"] == -1 and g["bottom"] <= c[i] <= g["top"] for g in gaps)

    return pd.DataFrame(
        {
            "fvg_bull_new": bull_new,
            "fvg_bear_new": bear_new,
            "fvg_bull_active": bull_cnt,
            "fvg_bear_active": bear_cnt,
            "in_bull_fvg": in_bull,
            "in_bear_fvg": in_bear,
        },
        index=df.index,
    )
