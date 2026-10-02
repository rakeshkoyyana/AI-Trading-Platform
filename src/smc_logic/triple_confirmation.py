"""
Triple Confirmation Strategy (EMA trend + RSI momentum + volume spike).

Ported 1:1 from the first half of the Pine script.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.smc_logic.config import SMCConfig
from src.smc_logic.indicators import ema, rsi, sma


def triple_confirmation_frame(df: pd.DataFrame, cfg: SMCConfig | None = None) -> pd.DataFrame:
    """Indicators + the three confirmations + entry/exit conditions, per bar."""
    cfg = cfg or SMCConfig()
    close = df["close"].to_numpy(float)
    volume = df["volume"].to_numpy(float)

    fast = ema(close, cfg.fast_ema)
    slow = ema(close, cfg.slow_ema)
    r = rsi(close, cfg.rsi_length)
    vavg = sma(volume, cfg.volume_sma)

    with np.errstate(invalid="ignore"):
        trend_bull = fast > slow
        trend_bear = fast < slow
        mom_bull = (r > 50) & (r < cfg.rsi_overbought)
        mom_bear = (r < 50) & (r > cfg.rsi_oversold)
        vol_spike = volume > vavg * cfg.volume_mult  # identical for long & short in the Pine source
        vol_ok = vol_spike if cfg.volume_filter else np.ones(len(df), dtype=bool)

        long_cond = trend_bull & mom_bull & vol_ok
        short_cond = trend_bear & mom_bear & vol_ok
        exit_long = trend_bear | (r >= cfg.rsi_overbought)
        exit_short = trend_bull | (r <= cfg.rsi_oversold)

    return pd.DataFrame(
        {
            "ema_fast": fast,
            "ema_slow": slow,
            "rsi": r,
            "vol_avg": vavg,
            "vol_ratio": volume / vavg,
            "trend_bull": trend_bull,
            "trend_bear": trend_bear,
            "mom_bull": mom_bull,
            "mom_bear": mom_bear,
            "vol_spike": vol_spike,
            "long_cond": long_cond,
            "short_cond": short_cond,
            "exit_long": exit_long,
            "exit_short": exit_short,
        },
        index=df.index,
    )


def get_signal(df: pd.DataFrame, cfg: SMCConfig | None = None) -> pd.Series:
    """Per-bar level signal: 'long' | 'short' | 'none' (matches the Pine entry conditions)."""
    f = triple_confirmation_frame(df, cfg)
    sig = np.where(f["long_cond"], "long", np.where(f["short_cond"], "short", "none"))
    return pd.Series(sig, index=df.index, name="signal")


def signal_events(df: pd.DataFrame, cfg: SMCConfig | None = None) -> pd.Series:
    """Rising edge of get_signal(): the first bar where long/short becomes true.

    These are the discrete signals stored in the DB and labelled for ML.
    """
    sig = get_signal(df, cfg)
    changed = sig != sig.shift(1, fill_value="none")
    return sig.where(changed & (sig != "none"), "none").rename("signal_event")


def simulate_strategy(df: pd.DataFrame, cfg: SMCConfig | None = None) -> pd.DataFrame:
    """Reproduce the Pine strategy's trade list (for comparison with TradingView's
    "List of trades"). Orders placed at the close of bar i fill at the open of bar i+1.

    Pine order of registration each bar: entry long, entry short, close long, close short.
    `strategy.entry` in the opposite direction reverses the position.
    Returns columns: direction, signal_bar, entry_time, entry_price, exit_time, exit_price.
    """
    f = triple_confirmation_frame(df, cfg)
    ts = df["timestamp"].to_numpy() if "timestamp" in df.columns else df.index.to_numpy()
    o = df["open"].to_numpy(float)
    n = len(df)

    pos = 0  # +1 long, -1 short, 0 flat
    trades: list[dict] = []
    open_trade: dict | None = None

    def close_trade(i_fill: int):
        nonlocal open_trade, pos
        if open_trade is not None:
            open_trade.update(exit_time=ts[i_fill], exit_price=o[i_fill])
            trades.append(open_trade)
        open_trade, pos = None, 0

    def open_new(direction: int, i_signal: int, i_fill: int):
        nonlocal open_trade, pos
        open_trade = dict(
            direction="long" if direction == 1 else "short",
            signal_bar=i_signal,
            entry_time=ts[i_fill],
            entry_price=o[i_fill],
            exit_time=pd.NaT,
            exit_price=np.nan,
        )
        pos = direction

    for i in range(n - 1):
        orders = []
        if f["long_cond"].iloc[i]:
            orders.append(("entry", 1))
        if f["short_cond"].iloc[i]:
            orders.append(("entry", -1))
        if f["exit_long"].iloc[i]:
            orders.append(("close", 1))
        if f["exit_short"].iloc[i]:
            orders.append(("close", -1))
        fill = i + 1
        for kind, d in orders:
            if kind == "entry":
                if pos == d:
                    continue
                if pos == -d:
                    close_trade(fill)
                open_new(d, i, fill)
            else:  # close the named-direction position if it is the one open
                if pos == d:
                    close_trade(fill)

    if open_trade is not None:
        trades.append(open_trade)  # still open at the end of data
    return pd.DataFrame(
        trades,
        columns=["direction", "signal_bar", "entry_time", "entry_price", "exit_time", "exit_price"],
    )
