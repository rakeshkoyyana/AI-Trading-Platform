"""
Feature engineering for the signal-quality model.

`row_features()` is the SINGLE source of truth for features: training builds them from stored
Signal rows, and the live decision engine builds them from the latest context bar. Using one
function for both removes train/serve skew.

Direction-aware ("aligned") features express context relative to the trade direction, so one
model serves longs and shorts: e.g. swing_aligned = +1 means structure supports the trade.
"""
from __future__ import annotations

import bisect
import math
from datetime import datetime

import numpy as np
import pandas as pd
from sqlalchemy import select

from src.db.schema import Signal, SentimentScore, get_engine, session_scope
from src.sentiment.aggregate import weighted_sentiment

MAX_AGE = 500.0  # cap for "bars since event" features

FEATURES = [
    "direction_long",
    "rsi", "vol_ratio", "ema_gap_aligned", "atr_pct", "range_atr",
    "swing_aligned", "int_aligned",
    "last_swing_aligned", "last_swing_is_choch", "bars_since_swing_event",
    "last_int_aligned", "last_int_is_choch", "bars_since_int_event",
    "pd_edge", "zone_premium", "zone_discount",
    "in_ob_support", "in_ob_oppose", "ob_dist_support_atr", "ob_dist_oppose_atr",
    "ob_support_count", "ob_oppose_count",
    "in_fvg_support", "in_fvg_oppose", "fvg_support_active",
    "minutes_since_open", "day_of_week",
    "sent_aligned", "sent_n",
]


def _f(x) -> float:
    if x is None:
        return float("nan")
    try:
        v = float(x)
    except (TypeError, ValueError):
        return float("nan")
    return v


def _nz_age(x) -> float:
    v = _f(x)
    return MAX_AGE if math.isnan(v) else min(v, MAX_AGE)


def row_features(
    details: dict,
    direction: str,
    timestamp: datetime,
    sentiment: dict | None = None,
) -> dict[str, float]:
    """Build the model's feature dict for one signal.

    details   : context values at the signal bar (FEATURE_COLS + 'zone'), as stored in the DB
    direction : 'long' | 'short'
    timestamp : naive-UTC bar timestamp
    sentiment : {'score': float, 'n': int} rolling sentiment as of the signal time (optional)
    """
    long = direction == "long"
    d = 1.0 if long else -1.0
    ts = pd.Timestamp(timestamp).tz_localize("UTC").tz_convert("America/New_York")
    minutes_since_open = ts.hour * 60 + ts.minute - (9 * 60 + 30)
    zone = details.get("zone", "unknown")
    pd_pos = _f(details.get("pd_position"))
    sent = sentiment or {}

    sup, opp = ("bull", "bear") if long else ("bear", "bull")
    feats = {
        "direction_long": 1.0 if long else 0.0,
        "rsi": _f(details.get("rsi")),
        "vol_ratio": _f(details.get("vol_ratio")),
        "ema_gap_aligned": _f(details.get("ema_gap_pct")) * d,
        "atr_pct": _f(details.get("atr_pct")),
        "range_atr": _f(details.get("range_atr")),
        "swing_aligned": _f(details.get("swing_trend")) * d,
        "int_aligned": _f(details.get("internal_trend")) * d,
        "last_swing_aligned": _f(details.get("last_swing_dir")) * d,
        "last_swing_is_choch": _f(details.get("last_swing_is_choch")),
        "bars_since_swing_event": _nz_age(details.get("bars_since_swing_event")),
        "last_int_aligned": _f(details.get("last_int_dir")) * d,
        "last_int_is_choch": _f(details.get("last_int_is_choch")),
        "bars_since_int_event": _nz_age(details.get("bars_since_int_event")),
        "pd_edge": (0.5 - pd_pos) * d if not math.isnan(pd_pos) else float("nan"),
        "zone_premium": 1.0 if zone == "premium" else 0.0,
        "zone_discount": 1.0 if zone == "discount" else 0.0,
        "in_ob_support": _f(details.get(f"in_{sup}_ob")),
        "in_ob_oppose": _f(details.get(f"in_{opp}_ob")),
        "ob_dist_support_atr": _f(details.get("dist_bull_ob_atr" if long else "dist_bear_ob_atr")),
        "ob_dist_oppose_atr": _f(details.get("dist_bear_ob_atr" if long else "dist_bull_ob_atr")),
        "ob_support_count": _f(details.get(f"ob_{sup}_count")),
        "ob_oppose_count": _f(details.get(f"ob_{opp}_count")),
        "in_fvg_support": _f(details.get(f"in_{sup}_fvg")),
        "in_fvg_oppose": _f(details.get(f"in_{opp}_fvg")),
        "fvg_support_active": _f(details.get(f"fvg_{sup}_active")),
        "minutes_since_open": float(minutes_since_open),
        "day_of_week": float(ts.dayofweek),
        "sent_aligned": float(sent.get("score", 0.0)) * d,
        "sent_n": float(sent.get("n", 0)),
    }
    return {k: feats[k] for k in FEATURES}


def build_feature_matrix(
    engine=None,
    timeframe: str | None = None,
    symbols: list[str] | None = None,
    labelled_only: bool = True,
    sentiment_window_hours: float = 4.0,
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """(X, y, meta) from the signals table.

    meta carries symbol, timestamp, direction, forward_return for evaluation.
    Historical rows normally have no sentiment (news APIs are not backfilled) -> sent_n = 0;
    the model only learns from sentiment as live-collected news accumulates.
    """
    engine = engine or get_engine()
    with session_scope(engine) as s:
        q = select(Signal)
        if timeframe:
            q = q.where(Signal.timeframe == timeframe)
        if symbols:
            q = q.where(Signal.symbol.in_(symbols))
        if labelled_only:
            q = q.where(Signal.label_win.is_not(None))
        sigs = list(s.execute(q.order_by(Signal.timestamp)).scalars())
        sent_rows = s.execute(
            select(SentimentScore.symbol, SentimentScore.timestamp, SentimentScore.score)
        ).all()

    by_sym: dict[str, tuple[list[datetime], list[float]]] = {}
    for sym, ts, sc in sorted(sent_rows, key=lambda r: (r[0], r[1])):
        by_sym.setdefault(sym, ([], []))
        by_sym[sym][0].append(ts)
        by_sym[sym][1].append(sc)

    rows, ys, metas = [], [], []
    for sg in sigs:
        sent = None
        if sg.symbol in by_sym:
            tss, scs = by_sym[sg.symbol]
            lo = bisect.bisect_left(tss, sg.timestamp - pd.Timedelta(hours=sentiment_window_hours))
            hi = bisect.bisect_right(tss, sg.timestamp)
            sent = weighted_sentiment(list(zip(tss[lo:hi], scs[lo:hi])), sg.timestamp, sentiment_window_hours)
        rows.append(row_features(sg.confirmation_details_json or {}, sg.direction, sg.timestamp, sent))
        ys.append(sg.label_win)
        metas.append(
            dict(
                signal_id=sg.id, symbol=sg.symbol, timestamp=sg.timestamp,
                direction=sg.direction, forward_return=sg.forward_return,
            )
        )
    X = pd.DataFrame(rows, columns=FEATURES)
    y = pd.Series(ys, name="label_win", dtype="float").astype("Int64") if ys else pd.Series(dtype="Int64")
    return X, y, pd.DataFrame(metas)
