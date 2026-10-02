"""
Compare the Python SMC port against a TradingView "Export chart data" CSV.

The CSV must come from a chart that has your Pine script plus the plot block in
`docs/tradingview_export_patch.pine` appended (see docs/TRADINGVIEW_VALIDATION.md).
Because the OHLCV in the CSV is TradingView's own, any difference is logic, not data.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.smc_logic.config import SMCConfig
from src.smc_logic.pipeline import compute_context

# TradingView plot title -> Python context column
EVENT_MAP = {
    "int_bull_bos": "int_bull_bos",
    "int_bull_choch": "int_bull_choch",
    "int_bear_bos": "int_bear_bos",
    "int_bear_choch": "int_bear_choch",
    "swing_bull_bos": "swing_bull_bos",
    "swing_bull_choch": "swing_bull_choch",
    "swing_bear_bos": "swing_bear_bos",
    "swing_bear_choch": "swing_bear_choch",
    "ob_mit_int_bull": "ob_mit_int_bull",
    "ob_mit_int_bear": "ob_mit_int_bear",
    "ob_mit_swing_bull": "ob_mit_swing_bull",
    "ob_mit_swing_bear": "ob_mit_swing_bear",
    "eqh": "eqh",
    "eql": "eql",
    "fvg_bull_new": "fvg_bull_new",
    "fvg_bear_new": "fvg_bear_new",
    "long_cond": "long_cond",
    "short_cond": "short_cond",
}
# numeric series compared with a tolerance
VALUE_MAP = {
    "ema_fast": "ema_fast",
    "ema_slow": "ema_slow",
    "rsi": "rsi",
    "atr200": "atr200",
    "swing_high_lvl": "swing_high",
    "swing_low_lvl": "swing_low",
    "trail_top": "trail_top",
    "trail_bottom": "trail_bottom",
}
# Events whose match rate gates the "pass" verdict (the rest are reported only).
CORE_EVENTS = [
    "swing_bull_bos", "swing_bull_choch", "swing_bear_bos", "swing_bear_choch",
    "int_bull_bos", "int_bull_choch", "int_bear_bos", "int_bear_choch",
    "long_cond", "short_cond",
]


@dataclass
class EventReport:
    name: str
    tv_count: int
    py_count: int
    matched: int
    matched_within_1bar: int
    tv_only: list
    py_only: list

    @property
    def agreement(self) -> float:
        union = self.tv_count + self.py_count - self.matched
        return 1.0 if union == 0 else self.matched / union

    @property
    def agreement_1bar(self) -> float:
        union = self.tv_count + self.py_count - self.matched_within_1bar
        return 1.0 if union == 0 else self.matched_within_1bar / union


def load_tradingview_csv(path_or_df) -> pd.DataFrame:
    """Read a TradingView export and normalise to lowercase columns + naive-UTC `timestamp`."""
    raw = path_or_df if isinstance(path_or_df, pd.DataFrame) else pd.read_csv(path_or_df)
    df = raw.copy()
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    if "time" not in df.columns:
        raise ValueError("export has no 'time' column")
    t = df["time"]
    if pd.api.types.is_numeric_dtype(t):
        ts = pd.to_datetime(t, unit="s", utc=True)
    else:
        ts = pd.to_datetime(t, utc=True)
    df["timestamp"] = ts.dt.tz_localize(None)
    if "volume" not in df.columns:
        for alt in ("vol", "volume_ma"):
            if alt in df.columns:
                df["volume"] = df[alt]
    need = ["open", "high", "low", "close", "volume"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise ValueError(f"export is missing columns: {missing}")
    return df.sort_values("timestamp", kind="stable").reset_index(drop=True)


def _flags(series: pd.Series) -> np.ndarray:
    return (pd.to_numeric(series, errors="coerce").fillna(0).to_numpy() > 0.5)


def compare_events(tv: pd.DataFrame, ctx: pd.DataFrame) -> list[EventReport]:
    reports = []
    for tv_col, py_col in EVENT_MAP.items():
        if tv_col not in tv.columns:
            continue
        a, b = _flags(tv[tv_col]), ctx[py_col].to_numpy(bool)
        ai, bi = np.where(a)[0], np.where(b)[0]
        matched = int((a & b).sum())
        near = 0
        used: set[int] = set()
        for i in ai:
            for j in (i, i - 1, i + 1):
                if 0 <= j < len(b) and b[j] and j not in used:
                    used.add(j)
                    near += 1
                    break
        ts = tv["timestamp"]
        reports.append(
            EventReport(
                name=tv_col,
                tv_count=len(ai),
                py_count=len(bi),
                matched=matched,
                matched_within_1bar=near,
                tv_only=[str(ts.iat[i]) for i in np.where(a & ~b)[0][:10]],
                py_only=[str(ts.iat[i]) for i in np.where(b & ~a)[0][:10]],
            )
        )
    return reports


def compare_values(tv: pd.DataFrame, ctx: pd.DataFrame, warmup: int = 300) -> dict[str, dict]:
    out = {}
    for tv_col, py_col in VALUE_MAP.items():
        if tv_col not in tv.columns:
            continue
        a = pd.to_numeric(tv[tv_col], errors="coerce").to_numpy(float)[warmup:]
        b = ctx[py_col].to_numpy(float)[warmup:]
        ok = ~np.isnan(a) & ~np.isnan(b)
        if ok.sum() == 0:
            out[tv_col] = dict(compared=0, max_abs_diff=np.nan, pct_within_1e3=np.nan)
            continue
        diff = np.abs(a[ok] - b[ok])
        scale = np.maximum(np.abs(a[ok]), 1e-9)
        out[tv_col] = dict(
            compared=int(ok.sum()),
            max_abs_diff=float(diff.max()),
            pct_within_1e3=float((diff / scale < 1e-3).mean() * 100),
        )
    return out


def validate(path_or_df, cfg: SMCConfig | None = None, warmup: int = 300) -> dict:
    tv = load_tradingview_csv(path_or_df)
    ctx = compute_context(tv[["timestamp", "open", "high", "low", "close", "volume"]], cfg)
    events = compare_events(tv, ctx)
    values = compare_values(tv, ctx, warmup)
    core = [r for r in events if r.name in CORE_EVENTS and (r.tv_count + r.py_count) > 0]
    overall = (
        sum(r.matched for r in core) / max(1, sum(r.tv_count + r.py_count - r.matched for r in core))
        if core
        else float("nan")
    )
    return dict(events=events, values=values, overall_agreement=overall, n_bars=len(tv), ctx=ctx, tv=tv)


def format_report(res: dict) -> str:
    lines = [f"Bars compared: {res['n_bars']}", ""]
    lines.append(f"{'event':<20}{'TV':>6}{'Py':>6}{'match':>7}{'agree':>8}{'±1bar':>8}")
    for r in res["events"]:
        lines.append(
            f"{r.name:<20}{r.tv_count:>6}{r.py_count:>6}{r.matched:>7}"
            f"{r.agreement * 100:>7.1f}%{r.agreement_1bar * 100:>7.1f}%"
        )
    lines.append("")
    if res["values"]:
        lines.append(f"{'series':<18}{'compared':>9}{'max |diff|':>14}{'% within 0.1%':>15}")
        for k, v in res["values"].items():
            lines.append(f"{k:<18}{v['compared']:>9}{v['max_abs_diff']:>14.6f}{v['pct_within_1e3']:>14.1f}%")
        lines.append("")
    for r in res["events"]:
        if r.tv_only or r.py_only:
            lines.append(f"[{r.name}] TV-only: {r.tv_only[:5]}  Py-only: {r.py_only[:5]}")
    ov = res["overall_agreement"]
    verdict = "PASS" if ov == ov and ov >= 0.90 else "REVIEW"
    lines += ["", f"Core-event agreement: {ov * 100:.1f}%  ->  {verdict}  (target >= 90%)"]
    return "\n".join(lines)
