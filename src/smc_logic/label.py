"""Label signals with forward outcomes for ML training."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.smc_logic.levels import stop_target_levels


def label_signals(
    ctx: pd.DataFrame,
    horizon: int = 8,
    min_return: float = 0.0,
    use_barriers: bool = True,
) -> pd.DataFrame:
    """One row per signal event with entry price and outcome.

    Entry = open of the bar after the signal bar (how the Pine strategy fills).
    Outcome, two flavours:
      * horizon return: direction * (close[entry+horizon-1] / entry - 1)
      * barrier outcome (use_barriers): walk bars from the entry bar; stop/target come from
        `stop_target_levels` at the signal bar. First touch wins; if both touch in the same bar
        the stop is assumed hit first (conservative). Unresolved at the horizon -> horizon return.
    `label_win` = 1 if the outcome return > min_return else 0. Rows without enough forward
    data are returned with NaN outcome so they can be excluded from training.
    """
    n = len(ctx)
    o = ctx["open"].to_numpy(float)
    h = ctx["high"].to_numpy(float)
    l = ctx["low"].to_numpy(float)
    c = ctx["close"].to_numpy(float)
    rows = []
    idxs = np.where(ctx["signal_event"].to_numpy() != "none")[0]
    for i in idxs:
        d = ctx["signal_event"].iat[i]
        row = dict(
            bar=int(i),
            timestamp=ctx["timestamp"].iat[i],
            direction=d,
            entry_price=np.nan,
            forward_return=np.nan,
            label_win=np.nan,
            exit_reason=None,
            bars_held=np.nan,
        )
        if i + horizon >= n:
            rows.append(row)
            continue
        entry = o[i + 1]
        sign = 1.0 if d == "long" else -1.0
        lv = stop_target_levels(ctx.iloc[i], d, entry=entry) if use_barriers else None
        exit_px, reason, held = c[i + horizon], "horizon", horizon
        if lv is not None:
            for k in range(i + 1, i + 1 + horizon):
                hit_stop = l[k] <= lv.stop if d == "long" else h[k] >= lv.stop
                hit_tgt = h[k] >= lv.target if d == "long" else l[k] <= lv.target
                if hit_stop:  # stop first when both touch
                    exit_px, reason, held = lv.stop, "stop", k - i
                    break
                if hit_tgt:
                    exit_px, reason, held = lv.target, "target", k - i
                    break
        ret = sign * (exit_px / entry - 1.0)
        row.update(
            entry_price=float(entry),
            forward_return=float(ret),
            label_win=int(ret > min_return),
            exit_reason=reason,
            bars_held=held,
        )
        rows.append(row)
    return pd.DataFrame(rows)
