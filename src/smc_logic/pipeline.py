"""Combine every SMC + triple-confirmation output into one per-bar context frame."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.smc_logic.config import PIPELINE_CONFIG, SMCConfig
from src.smc_logic.fvg import detect_fvg
from src.smc_logic.indicators import atr as _atr
from src.smc_logic.order_blocks import detect_order_blocks
from src.smc_logic.structure import detect_structure
from src.smc_logic.triple_confirmation import (
    get_signal,
    signal_events,
    triple_confirmation_frame,
)
from src.smc_logic.zones import detect_premium_discount

SWING_EVENT_COLS = ["swing_bull_bos", "swing_bull_choch", "swing_bear_bos", "swing_bear_choch"]
INT_EVENT_COLS = ["int_bull_bos", "int_bull_choch", "int_bear_bos", "int_bear_choch"]


def _bars_since(flag: np.ndarray) -> np.ndarray:
    """Bars since the last True (0 on the bar itself); NaN if never."""
    out = np.full(len(flag), np.nan)
    last = None
    for i, f in enumerate(flag):
        if f:
            last = i
        if last is not None:
            out[i] = i - last
    return out


def _last_event(frame: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Direction (+1/-1), is_choch and age of the most recent event among `cols`."""
    n = len(frame)
    direction = np.zeros(n, dtype=int)
    is_choch = np.zeros(n, dtype=bool)
    age = np.full(n, np.nan)
    cur_dir, cur_choch, last_i = 0, False, None
    for i in range(n):
        for col in cols:
            if frame[col].iat[i]:
                cur_dir = 1 if "bull" in col else -1
                cur_choch = col.endswith("choch")
                last_i = i
        direction[i], is_choch[i] = cur_dir, cur_choch
        if last_i is not None:
            age[i] = i - last_i
    return pd.DataFrame({"dir": direction, "is_choch": is_choch, "age": age}, index=frame.index)


def compute_context(df: pd.DataFrame, cfg: SMCConfig | None = None) -> pd.DataFrame:
    """Full per-bar context for one symbol/timeframe.

    `df` needs columns timestamp, open, high, low, close, volume (ascending).
    Everything at row i uses only bars <= i.
    """
    cfg = cfg or PIPELINE_CONFIG
    df = df.reset_index(drop=True)
    st = detect_structure(df, cfg)
    ob = detect_order_blocks(df, cfg, structure=st)
    fvg = detect_fvg(df, cfg)
    zones = detect_premium_discount(st.frame, df["close"])
    tc = triple_confirmation_frame(df, cfg)

    ctx = pd.concat(
        [df[["timestamp", "open", "high", "low", "close", "volume"]], st.frame, ob, fvg, zones, tc],
        axis=1,
    )
    ctx["atr"] = _atr(df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float), 14)
    ctx["signal"] = get_signal(df, cfg).to_numpy()
    ctx["signal_event"] = signal_events(df, cfg).to_numpy()

    sw = _last_event(ctx, SWING_EVENT_COLS)
    it = _last_event(ctx, INT_EVENT_COLS)
    ctx["last_swing_dir"], ctx["last_swing_is_choch"], ctx["bars_since_swing_event"] = (
        sw["dir"], sw["is_choch"], sw["age"],
    )
    ctx["last_int_dir"], ctx["last_int_is_choch"], ctx["bars_since_int_event"] = (
        it["dir"], it["is_choch"], it["age"],
    )

    with np.errstate(invalid="ignore", divide="ignore"):
        ctx["dist_bull_ob_atr"] = (ctx["close"] - ctx["bull_ob_high"]) / ctx["atr"]
        ctx["dist_bear_ob_atr"] = (ctx["bear_ob_low"] - ctx["close"]) / ctx["atr"]
        ctx["range_atr"] = (ctx["high"] - ctx["low"]) / ctx["atr"]
        ctx["ema_gap_pct"] = (ctx["ema_fast"] - ctx["ema_slow"]) / ctx["close"]
        ctx["atr_pct"] = ctx["atr"] / ctx["close"]
    return ctx


# Numeric/boolean context columns stored with each signal and used as ML features.
FEATURE_COLS = [
    "rsi", "vol_ratio", "ema_gap_pct", "atr_pct", "range_atr",
    "swing_trend", "internal_trend",
    "last_swing_dir", "last_swing_is_choch", "bars_since_swing_event",
    "last_int_dir", "last_int_is_choch", "bars_since_int_event",
    "pd_position", "in_bull_ob", "in_bear_ob", "dist_bull_ob_atr", "dist_bear_ob_atr",
    "ob_bull_count", "ob_bear_count",
    "fvg_bull_active", "fvg_bear_active", "in_bull_fvg", "in_bear_fvg",
]
