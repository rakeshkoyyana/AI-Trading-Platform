"""Pine-compatible indicator primitives (ta.rma / ta.ema / ta.rsi / ta.atr / ta.sma).

Pine seeds its recursive averages with the SMA of the first `length` valid values and
returns `na` before that. pandas' ewm(adjust=False) seeds differently, so these are
implemented explicitly to match TradingView bar-for-bar.
"""
from __future__ import annotations

import numpy as np


def sma(x: np.ndarray, length: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) < length:
        return out
    c = np.cumsum(np.insert(x, 0, 0.0))
    out[length - 1 :] = (c[length:] - c[:-length]) / length
    # any NaN inside the window poisons the result, as in Pine
    nan_win = np.convolve(np.isnan(x).astype(float), np.ones(length), mode="valid") > 0
    out[length - 1 :][nan_win] = np.nan
    return out


def _seeded_recursive(x: np.ndarray, length: int, alpha: float) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    valid = np.where(~np.isnan(x))[0]
    if len(valid) < length:
        return out
    first = valid[0]
    seed_idx = first + length - 1
    if seed_idx >= len(x):
        return out
    out[seed_idx] = np.mean(x[first : seed_idx + 1])
    for i in range(seed_idx + 1, len(x)):
        v = x[i]
        prev = out[i - 1]
        out[i] = prev if np.isnan(v) else alpha * v + (1.0 - alpha) * prev
    return out


def ema(x: np.ndarray, length: int) -> np.ndarray:
    return _seeded_recursive(x, length, 2.0 / (length + 1.0))


def rma(x: np.ndarray, length: int) -> np.ndarray:
    return _seeded_recursive(x, length, 1.0 / length)


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    high, low, close = (np.asarray(a, dtype=float) for a in (high, low, close))
    tr = high - low
    if len(tr) > 1:
        pc = close[:-1]
        tr[1:] = np.maximum.reduce([high[1:] - low[1:], np.abs(high[1:] - pc), np.abs(low[1:] - pc)])
    return tr


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, length: int) -> np.ndarray:
    return rma(true_range(high, low, close), length)


def rsi(close: np.ndarray, length: int = 14) -> np.ndarray:
    close = np.asarray(close, dtype=float)
    change = np.full(len(close), np.nan)
    change[1:] = np.diff(close)
    up = rma(np.where(np.isnan(change), np.nan, np.maximum(change, 0.0)), length)
    down = rma(np.where(np.isnan(change), np.nan, np.maximum(-change, 0.0)), length)
    out = np.full(len(close), np.nan)
    ok = ~np.isnan(up) & ~np.isnan(down)
    # Pine: down == 0 ? 100 : up == 0 ? 0 : 100 - 100/(1+up/down)
    down_zero = ok & (down == 0)
    up_zero = ok & ~down_zero & (up == 0)
    normal = ok & ~down_zero & ~up_zero
    out[normal] = 100.0 - 100.0 / (1.0 + up[normal] / down[normal])
    out[down_zero] = 100.0
    out[up_zero] = 0.0
    return out
