"""Shadow "analyst council": rule-based analyst votes on every fresh signal. Free, deterministic, advisory only.

Each analyst votes +1 (supports the signal's direction), -1 (opposes) or 0 (neutral), relative to the signal. The
votes are logged next to the real decision and NEVER change it. After a few weeks of paper trading,
`summarize_votes` shows whether agreement actually predicts wins, and only then is it worth promoting.

The verdict thresholds are fixed up front (not tuned on results):  mean vote >= 0.34 -> "agree",
<= -0.20 -> "disagree", otherwise "mixed".
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy import select

from src.db.schema import CouncilVote, Signal, session_scope

ANALYSTS = ("momentum", "volume", "structure", "zone", "blocks", "htf", "sentiment")
LABELS = dict(momentum="Momentum room", volume="Volume", structure="Structure", zone="Premium/discount",
              blocks="Order block / FVG", htf="Higher timeframes", sentiment="Sentiment")
AGREE_AT, DISAGREE_AT = 0.34, -0.20


def _num(x, default=np.nan) -> float:
    try:
        v = float(x)
        return default if np.isnan(v) else v
    except (TypeError, ValueError):
        return default


def feature_votes(f: dict, direction: str) -> dict[str, tuple[int, str]]:
    """Votes that depend only on the stored signal features (so they can also be graded on history)."""
    s = 1 if direction == "long" else -1
    out: dict[str, tuple[int, str]] = {}

    rsi = _num(f.get("rsi"))
    if not np.isnan(rsi):
        room = (70 - rsi) if s == 1 else (rsi - 30)  # distance to the Pine RSI exit limit
        out["momentum"] = (1, f"{room:.0f} RSI pts before the exit limit") if room >= 15 else \
            ((-1, f"only {room:.0f} RSI pts before the exit limit") if room < 8 else (0, f"{room:.0f} RSI pts of room"))

    vr = _num(f.get("vol_ratio"))
    if not np.isnan(vr):
        out["volume"] = (1, f"volume {vr:.1f}x average") if vr >= 2.0 else (0, f"volume {vr:.1f}x average")

    st, it = _num(f.get("swing_trend"), 0), _num(f.get("internal_trend"), 0)
    sc = int(np.sign(st)) * s + int(np.sign(it)) * s
    out["structure"] = (1, "swing and/or internal structure agree") if sc > 0 else \
        ((-1, "structure leans against the trade") if sc < 0 else (0, "structure neutral"))

    pdp = _num(f.get("pd_position"))
    if not np.isnan(pdp):
        a = pdp if s == -1 else 1 - pdp  # 1 = best place to be (discount for longs, premium for shorts)
        out["zone"] = (1, f"{'discount' if s == 1 else 'premium'} (range position {pdp:.2f})") if a >= 0.6 else \
            ((-1, f"{'premium' if s == 1 else 'discount'} (range position {pdp:.2f})") if a <= 0.3 else (0, f"mid-range ({pdp:.2f})"))

    bull = bool(f.get("in_bull_ob")) or bool(f.get("in_bull_fvg"))
    bear = bool(f.get("in_bear_ob")) or bool(f.get("in_bear_fvg"))
    mine, theirs = (bull, bear) if s == 1 else (bear, bull)
    out["blocks"] = (1, "price sits in a supportive order block / FVG") if mine and not theirs else \
        ((-1, "price sits in an opposing order block / FVG") if theirs and not mine else (0, "no block / FVG decides it"))
    return out


def htf_vote(bars: pd.DataFrame, direction: str) -> tuple[int, str] | None:
    """1H and 4H EMA 9/21 trend vs the signal (live only: needs the bar history)."""
    from src.dashboard.data import resample_bars
    from src.smc_logic.indicators import ema

    s = 1 if direction == "long" else -1
    trends = {}
    for tf, label in (("1Hour", "1H"), ("4Hour", "4H")):
        try:
            b = resample_bars(bars, tf)
        except Exception:  # noqa: BLE001
            continue
        if len(b) >= 30:
            c = b["close"].to_numpy(float)
            trends[label] = 1 if ema(c, 9)[-1] > ema(c, 21)[-1] else -1
    if not trends:
        return None
    agree = sum(1 for t in trends.values() if t == s)
    txt = ", ".join(f"{k} {'up' if v == 1 else 'down'}" for k, v in trends.items())
    return (1, txt) if agree == len(trends) else ((-1, txt) if agree == 0 else (0, txt))


def sentiment_vote(sentiment: dict | None, direction: str) -> tuple[int, str] | None:
    if not sentiment or sentiment.get("n", 0) <= 0 or sentiment.get("score") is None:
        return None
    sc = float(sentiment["score"]) * (1 if direction == "long" else -1)
    txt = f"news sentiment {float(sentiment['score']):+.2f} ({sentiment['n']} items)"
    return (1, txt) if sc >= 0.15 else ((-1, txt) if sc <= -0.15 else (0, txt))


def tally(votes: dict[str, tuple[int, str]]) -> dict:
    vals = [v for v, _ in votes.values()]
    score = float(np.mean(vals)) if vals else 0.0
    verdict = "agree" if score >= AGREE_AT else ("disagree" if score <= DISAGREE_AT else "mixed")
    return dict(score=round(score, 3), verdict=verdict, n=len(vals),
                votes={k: dict(v=v, why=w) for k, (v, w) in votes.items()})


def council_for(features: dict, direction: str, bars: pd.DataFrame | None = None, sentiment: dict | None = None) -> dict:
    votes = feature_votes(features, direction)
    if bars is not None:
        h = htf_vote(bars, direction)
        if h:
            votes["htf"] = h
    sv = sentiment_vote(sentiment, direction)
    if sv:
        votes["sentiment"] = sv
    return tally(votes)


# ------------------------------------------------------------------ storage
def record_vote(engine, symbol: str, timeframe: str, signal_time: datetime, direction: str, result: dict,
                action: str, trade_id: int | None = None) -> None:
    with session_scope(engine) as s:
        row = s.execute(select(CouncilVote).where(CouncilVote.symbol == symbol, CouncilVote.signal_time == signal_time,
                                                  CouncilVote.direction == direction)).scalars().first()
        if row is None:
            row = CouncilVote(symbol=symbol, timeframe=timeframe, signal_time=signal_time, direction=direction,
                              created_at=datetime.now(timezone.utc).replace(tzinfo=None))
            s.add(row)
        row.votes_json, row.score, row.verdict, row.action = json.dumps(result["votes"]), result["score"], result["verdict"], action[:40]
        if trade_id:
            row.trade_id = trade_id


def get_vote(engine, symbol: str, signal_time: datetime) -> CouncilVote | None:
    with session_scope(engine) as s:
        row = s.execute(select(CouncilVote).where(CouncilVote.symbol == symbol, CouncilVote.signal_time == signal_time)
                        .order_by(CouncilVote.id.desc())).scalars().first()
        if row is not None:
            s.expunge(row)
        return row


# ------------------------------------------------------------------ grading
def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def logged_votes_frame(engine) -> pd.DataFrame:
    """Live shadow votes joined to the outcome label of their signal (label_win once forward bars exist)."""
    with session_scope(engine) as s:
        sigs = {(g.symbol, g.timestamp): g.label_win for g in s.execute(select(Signal)).scalars()}
        rows = []
        for v in s.execute(select(CouncilVote)).scalars():
            d = json.loads(v.votes_json or "{}")
            r = dict(symbol=v.symbol, time=v.signal_time, direction=v.direction, score=v.score, verdict=v.verdict,
                     action=v.action, label=sigs.get((v.symbol, v.signal_time)))
            r.update({f"vote_{k}": x["v"] for k, x in d.items()})
            rows.append(r)
    return _frame(rows)


def historical_votes_frame(engine) -> pd.DataFrame:
    """Feature-based votes (no HTF / sentiment) for every labelled stored signal: instant evidence from history."""
    with session_scope(engine) as s:
        rows = []
        for g in s.execute(select(Signal).where(Signal.label_win.is_not(None))).scalars():
            f = dict(g.confirmation_details_json or {})
            if not f:
                continue
            res = tally(feature_votes(f, g.direction))
            r = dict(symbol=g.symbol, time=g.timestamp, direction=g.direction, score=res["score"], verdict=res["verdict"],
                     action="history", label=g.label_win)
            r.update({f"vote_{k}": x["v"] for k, x in res["votes"].items()})
            rows.append(r)
    return _frame(rows)


def summarize_votes(df: pd.DataFrame) -> dict:
    """Win rate by council verdict and by individual analyst vote, over signals that have an outcome label."""
    out = dict(n=0, base_win=None, verdicts=pd.DataFrame(), analysts=pd.DataFrame())
    if df is None or df.empty or "label" not in df:
        return out
    d = df[df["label"].notna()].copy()
    if d.empty:
        return out
    d["label"] = d["label"].astype(float)
    out["n"], out["base_win"] = int(len(d)), float(d["label"].mean())
    v = d.groupby("verdict")["label"].agg(signals="count", win_rate="mean").reindex(["agree", "mixed", "disagree"]).dropna(how="all")
    v["lift_vs_all"] = v["win_rate"] - out["base_win"]
    out["verdicts"] = v.reset_index()
    rows = []
    for a in ANALYSTS:
        col = f"vote_{a}"
        if col not in d:
            continue
        for val, name in ((1, "supports"), (0, "neutral"), (-1, "opposes")):
            sub = d[d[col] == val]
            if len(sub):
                rows.append(dict(analyst=LABELS[a], vote=name, signals=int(len(sub)), win_rate=float(sub["label"].mean())))
    out["analysts"] = pd.DataFrame(rows)
    return out
