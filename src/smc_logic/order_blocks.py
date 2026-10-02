"""
Order blocks: stored on structure breaks, removed on mitigation.

Consumes the events emitted by `structure.detect_structure`. Bar order matches Pine:
an OB stored on bar i can be mitigated on that same bar (store happens in
displayStructure, delete runs right after).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.smc_logic.config import SMCConfig
from src.smc_logic.structure import BEARISH, BULLISH, StructureResult, detect_structure


def detect_order_blocks(
    df: pd.DataFrame,
    cfg: SMCConfig | None = None,
    structure: StructureResult | None = None,
) -> pd.DataFrame:
    """Per-bar order-block context.

    Columns:
      ob_bull_count / ob_bear_count   active (unmitigated) OBs, internal + swing
      bull_ob_high / bull_ob_low      nearest active bullish OB at/below price (NaN if none)
      bear_ob_high / bear_ob_low      nearest active bearish OB at/above price (NaN if none)
      in_bull_ob / in_bear_ob         close is inside such an OB
      ob_mit_{int,swing}_{bull,bear}  an OB of that kind was mitigated this bar
    """
    cfg = cfg or SMCConfig()
    st = structure or detect_structure(df, cfg)
    f = st.frame
    n = len(df)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    ph = f["parsed_high"].to_numpy(float)
    pl = f["parsed_low"].to_numpy(float)

    bear_src = c if cfg.ob_mitigation == "close" else h
    bull_src = c if cfg.ob_mitigation == "close" else l

    by_bar: dict[int, list[dict]] = {}
    for ev in st.events:
        by_bar.setdefault(ev["i"], []).append(ev)

    obs = {True: [], False: []}  # internal? -> list of dicts, newest first
    enabled = {True: cfg.internal_order_blocks, False: cfg.swing_order_blocks}

    cols = {
        k: np.full(n, np.nan)
        for k in ["bull_ob_high", "bull_ob_low", "bear_ob_high", "bear_ob_low"]
    }
    count_bull = np.zeros(n, dtype=int)
    count_bear = np.zeros(n, dtype=int)
    in_bull = np.zeros(n, dtype=bool)
    in_bear = np.zeros(n, dtype=bool)
    mit = {k: np.zeros(n, dtype=bool) for k in ["int_bull", "int_bear", "swing_bull", "swing_bear"]}

    for i in range(n):
        # store (internal then swing, like the two displayStructure calls)
        for ev in by_bar.get(i, []):
            internal = ev["internal"]
            if not enabled[internal]:
                continue
            lo_i = ev["pivot_bar"]
            if ev["bias"] == BEARISH:
                seg = ph[lo_i:i]
                if len(seg) == 0:
                    continue
                idx = lo_i + int(np.argmax(seg))
            else:
                seg = pl[lo_i:i]
                if len(seg) == 0:
                    continue
                idx = lo_i + int(np.argmin(seg))
            if len(obs[internal]) >= cfg.ob_max_stored:
                obs[internal].pop()
            obs[internal].insert(0, dict(high=ph[idx], low=pl[idx], bar=idx, bias=ev["bias"]))

        # mitigate
        for internal in (True, False):
            keep = []
            for ob in obs[internal]:
                crossed = False
                if ob["bias"] == BEARISH and bear_src[i] > ob["high"]:
                    crossed = True
                    mit["int_bear" if internal else "swing_bear"][i] = True
                elif ob["bias"] == BULLISH and bull_src[i] < ob["low"]:
                    crossed = True
                    mit["int_bull" if internal else "swing_bull"][i] = True
                if not crossed:
                    keep.append(ob)
            obs[internal] = keep

        active = obs[True] + obs[False]
        bulls = [o for o in active if o["bias"] == BULLISH]
        bears = [o for o in active if o["bias"] == BEARISH]
        count_bull[i], count_bear[i] = len(bulls), len(bears)
        below = [o for o in bulls if o["low"] <= c[i]]
        above = [o for o in bears if o["high"] >= c[i]]
        if below:
            nb = max(below, key=lambda o: o["low"])
            cols["bull_ob_high"][i], cols["bull_ob_low"][i] = nb["high"], nb["low"]
            in_bull[i] = nb["low"] <= c[i] <= nb["high"]
        if above:
            nb = min(above, key=lambda o: o["high"])
            cols["bear_ob_high"][i], cols["bear_ob_low"][i] = nb["high"], nb["low"]
            in_bear[i] = nb["low"] <= c[i] <= nb["high"]

    res = pd.DataFrame(
        {
            "ob_bull_count": count_bull,
            "ob_bear_count": count_bear,
            **cols,
            "in_bull_ob": in_bull,
            "in_bear_ob": in_bear,
            **{f"ob_mit_{k}": v for k, v in mit.items()},
        },
        index=df.index,
    )
    return res
