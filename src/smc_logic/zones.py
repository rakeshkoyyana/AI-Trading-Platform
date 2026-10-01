"""Premium / discount / equilibrium zones from the trailing swing extremes."""
from __future__ import annotations

import numpy as np
import pandas as pd


def detect_premium_discount(frame: pd.DataFrame, close: pd.Series) -> pd.DataFrame:
    """Classify each bar's close inside the trailing swing range.

    `frame` must contain trail_top / trail_bottom (from structure.detect_structure).
    Zone bounds follow the Pine drawPremiumDiscountZones():
      premium     [0.95*top + 0.05*bottom, top]
      discount    [bottom, 0.95*bottom + 0.05*top]
      equilibrium [0.525*bottom + 0.475*top, 0.525*top + 0.475*bottom]

    Columns: pd_position (0=bottom..1=top), zone in
             {premium, above_eq, equilibrium, below_eq, discount, unknown}
    """
    top = frame["trail_top"].to_numpy(float)
    bot = frame["trail_bottom"].to_numpy(float)
    c = close.to_numpy(float)

    rng = top - bot
    with np.errstate(divide="ignore", invalid="ignore"):
        pos = np.where(rng > 0, (c - bot) / rng, np.nan)

    prem_lo = 0.95 * top + 0.05 * bot
    disc_hi = 0.95 * bot + 0.05 * top
    eq_hi = 0.525 * top + 0.475 * bot
    eq_lo = 0.525 * bot + 0.475 * top
    mid = (top + bot) / 2.0

    zone = np.full(len(c), "unknown", dtype=object)
    valid = ~np.isnan(top) & ~np.isnan(bot)
    zone[valid & (c >= prem_lo)] = "premium"
    zone[valid & (c <= disc_hi)] = "discount"
    in_eq = valid & (c >= eq_lo) & (c <= eq_hi)
    zone[in_eq] = "equilibrium"
    rest = valid & ~(c >= prem_lo) & ~(c <= disc_hi) & ~in_eq
    zone[rest & (c > mid)] = "above_eq"
    zone[rest & (c <= mid)] = "below_eq"

    return pd.DataFrame({"pd_position": pos, "zone": zone}, index=close.index)
