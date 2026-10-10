"""Data shaping for the dashboard: chart payloads, watchlist/scanner rows, news and model cards.

Pure pandas/numpy (no Streamlit) so it is unit-testable. Timestamps in the DB are naive UTC;
the chart payload carries UTC epoch seconds and the browser formats them in America/Chicago.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import select

from src.config import PROJECT_ROOT
from src.data_ingestion.common import regular_hours_mask
from src.db.schema import News, SentimentScore, session_scope
from src.smc_logic.indicators import ema as _ema
from src.smc_logic.indicators import rsi as _rsi

TIMEFRAMES = {"5m": "5Min", "15m": "15Min", "30m": "30Min", "1H": "1Hour", "2H": "2Hour", "4H": "4Hour", "1D": "1Day"}
_WIDTH = {"30Min": 30, "1Hour": 60, "2Hour": 120, "4Hour": 240}
MIN_CTX_BARS = 120  # below this the SMC context is mostly warm-up noise, so no overlays are drawn
# bars shown per timeframe (the context is always computed on everything we have, for warm-up)
DISPLAY_BARS = {"5Min": 4500, "15Min": 2600, "30Min": 1800, "1Hour": 1500}
UP, DOWN, ACCENT, AMBER, MUTED = "#26a69a", "#ef5350", "#6c8cff", "#f5b041", "#8a90ab"
PURPLE = "#b061ff"  # a strategy CLOSE order (TradingView: "Close entry(s) order ...")
INITIAL_CAPITAL, QTY_PCT = 10_000, 0.10  # the Pine script's strategy() settings, used for the order-size labels
LONG_BLUE, SHORT_RED = "#4c8dff", "#ef5350"  # strategy entries: Long = blue arrow up, Short = red arrow down


def _epoch(ts) -> int:
    return int(pd.Timestamp(ts).tz_localize("UTC").timestamp())


def _f(x, nd=4):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), nd)


# --------------------------------------------------------------- resampling
def resample_bars(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Aggregate 15-minute bars into 30Min / 1Hour / 2Hour / 4Hour / 1Day bars.

    Intraday buckets are session-aware, like TradingView: regular-session buckets are anchored at 09:30 ET,
    pre-market at 04:00 and after-hours at 16:00, so a bucket never straddles two sessions.
    The bar is stamped with its bucket start (not the first print), and daily bars use the regular session only.
    """
    if tf in ("5Min", "15Min") or df.empty:
        return df.reset_index(drop=True)
    if tf == "1Day":
        rth = df[regular_hours_mask(df["timestamp"], "15Min")]
        if rth.empty:
            return rth.reset_index(drop=True)
        day = pd.to_datetime(rth["timestamp"]).dt.tz_localize("UTC").dt.tz_convert("America/New_York").dt.date
        out = rth.groupby(day, sort=True).agg(
            timestamp=("timestamp", "first"), open=("open", "first"), high=("high", "max"),
            low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
            **({"est": ("est", "max")} if "est" in rth.columns else {}),
        )
        return out.reset_index(drop=True)
    width = _WIDTH[tf]
    et = pd.to_datetime(df["timestamp"]).dt.tz_localize("UTC").dt.tz_convert("America/New_York")
    mins = (et.dt.hour * 60 + et.dt.minute).to_numpy()
    origin = np.where(mins < 570, 240, np.where(mins < 960, 570, 960))  # pre 04:00 | regular 09:30 | post 16:00
    offset = origin + ((mins - origin) // width) * width
    start = (et.dt.normalize() + pd.to_timedelta(offset, unit="m")).dt.tz_convert("UTC").dt.tz_localize(None)
    out = df.groupby(start.to_numpy(), sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
        **({"est": ("est", "max")} if "est" in df.columns else {}),
    )
    out.insert(0, "timestamp", pd.to_datetime(out.index))
    return out.reset_index(drop=True)


def _line(ts_ep: np.ndarray, vals: np.ndarray) -> list[dict]:
    return [{"time": int(t), "value": round(float(v), 4)} for t, v in zip(ts_ep, vals) if not np.isnan(v)]


def dataset_for(df: pd.DataFrame) -> dict:
    """Candles, volume, EMA9/21 and RSI14 for one timeframe."""
    if df.empty:
        return dict(candles=[], volume=[], ema9=[], ema21=[], rsi=[], est_from=None)
    t = np.array([_epoch(x) for x in df["timestamp"]])
    o, h, l, c, v = (df[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    est = df["est"].to_numpy(bool) if "est" in df.columns else np.zeros(len(df), dtype=bool)
    candles = []
    for i in range(len(df)):
        bar = dict(time=int(t[i]), open=round(o[i], 4), high=round(h[i], 4), low=round(l[i], 4), close=round(c[i], 4))
        if est[i]:  # live estimate (real-time IEX, rescaled): drawn faded until the exact SIP bar replaces it
            col = "rgba(38,166,154,.35)" if c[i] >= o[i] else "rgba(239,83,80,.35)"
            bar.update(color=col, borderColor=UP if c[i] >= o[i] else DOWN, wickColor=col)
        candles.append(bar)
    volume = [dict(time=int(t[i]), value=float(v[i]), color=("rgba(38,166,154,.45)" if c[i] >= o[i] else "rgba(239,83,80,.45)"))
              for i in range(len(df))]
    est_from = int(t[np.argmax(est)]) if est.any() else None
    return dict(candles=candles, volume=volume, ema9=_line(t, _ema(c, 9)), ema21=_line(t, _ema(c, 21)),
                rsi=_line(t, _rsi(c, 14)), est_from=est_from)


# ------------------------------------------------------------- SMC overlays
def _merge_zones(t_ep, hi, lo, color, label, max_n=7):
    """Run-length the 'nearest active OB' columns into discrete zones (merged by exact bounds)."""
    zones: dict[tuple, dict] = {}
    n = len(t_ep)
    for i in range(n):
        if np.isnan(hi[i]) or np.isnan(lo[i]):
            continue
        k = (round(float(hi[i]), 4), round(float(lo[i]), 4))
        z = zones.get(k)
        if z is None:
            zones[k] = dict(t0=int(t_ep[i]), t1=int(t_ep[i]), top=k[0], bottom=k[1], color=color, label=label, active=False)
        else:
            z["t1"] = int(t_ep[i])
    for z in zones.values():
        z["active"] = z["t1"] == int(t_ep[-1])
    out = sorted(zones.values(), key=lambda z: z["t0"])[-max_n:]
    return out


def _fvg_zones(ctx: pd.DataFrame, t_ep: np.ndarray, max_n: int = 5) -> list[dict]:
    h, l, c = ctx["high"].to_numpy(float), ctx["low"].to_numpy(float), ctx["close"].to_numpy(float)
    zones = []
    for kind in ("bull", "bear"):
        col = ctx[f"fvg_{kind}_new"].to_numpy(bool)
        for i in np.where(col)[0]:
            if i < 2:
                continue
            top, bot = (l[i], h[i - 2]) if kind == "bull" else (l[i - 2], h[i])
            if top <= bot:
                continue
            end, active = int(t_ep[-1]), True
            for j in range(i + 1, len(ctx)):  # mitigated when price trades through the far side
                if (kind == "bull" and l[j] < bot) or (kind == "bear" and h[j] > top):
                    end, active = int(t_ep[j]), False
                    break
            zones.append(dict(t0=int(t_ep[i - 2]), t1=end, top=round(float(top), 4), bottom=round(float(bot), 4),
                              color="rgba(38,166,154,.16)" if kind == "bull" else "rgba(239,83,80,.16)",
                              label=f"FVG {'↑' if kind == 'bull' else '↓'}", active=active, kind="fvg"))
    return sorted(zones, key=lambda z: z["t0"])[-max_n * 2:]


def _level_segments(t_ep, vals, color, label, max_n=14, dash=True):
    segs, start, cur = [], None, None
    for i, v in enumerate(vals):
        v = None if np.isnan(v) else round(float(v), 4)
        if v != cur:
            if cur is not None:
                segs.append(dict(t0=int(t_ep[start]), t1=int(t_ep[i - 1]), p=cur, color=color, label=label, dash=dash))
            start, cur = i, v
    if cur is not None:
        segs.append(dict(t0=int(t_ep[start]), t1=int(t_ep[-1]), p=cur, color=color, label=label, dash=dash, live=True))
    return segs[-max_n:]


def smc_overlays(ctx: pd.DataFrame) -> dict:
    """Zones, level lines and markers derived from the context frame (native timeframe)."""
    if ctx is None or len(ctx) < 5:
        return dict(zones=[], segs=[], markers=[])
    t_ep = np.array([_epoch(x) for x in ctx["timestamp"]])
    zones = []
    for z in _merge_zones(t_ep, ctx["bull_ob_high"].to_numpy(float), ctx["bull_ob_low"].to_numpy(float), "rgba(38,166,154,.22)", "Bull OB"):
        zones.append({**z, "kind": "ob"})
    for z in _merge_zones(t_ep, ctx["bear_ob_high"].to_numpy(float), ctx["bear_ob_low"].to_numpy(float), "rgba(239,83,80,.22)", "Bear OB"):
        zones.append({**z, "kind": "ob"})
    zones += _fvg_zones(ctx, t_ep)

    # premium / equilibrium / discount over the trailing swing range (LuxAlgo fractions)
    top, bot = float(ctx["trail_top"].iat[-1]), float(ctx["trail_bottom"].iat[-1])
    if not (math.isnan(top) or math.isnan(bot)) and top > bot:
        t0 = int(t_ep[max(len(t_ep) - 80, 0)])
        t1 = int(t_ep[-1])
        for lo_f, hi_f, col, lab in ((0.95, 1.0, "rgba(239,83,80,.10)", "Premium"), (0.475, 0.525, "rgba(138,144,171,.14)", "Equilibrium"), (0.0, 0.05, "rgba(38,166,154,.10)", "Discount")):
            zones.append(dict(t0=t0, t1=t1, top=round(bot + (top - bot) * hi_f, 4), bottom=round(bot + (top - bot) * lo_f, 4),
                              color=col, label=lab, active=True, kind="pd"))

    segs = (
        _level_segments(t_ep, ctx["swing_high"].to_numpy(float), "#ef9a9a", "Swing High")
        + _level_segments(t_ep, ctx["swing_low"].to_numpy(float), "#80cbc4", "Swing Low")
    )
    for s in segs:
        s["kind"] = "swing"

    markers = []
    h, l = ctx["high"].to_numpy(float), ctx["low"].to_numpy(float)
    ev = [("swing_bull_bos", "BOS", UP, "below"), ("swing_bear_bos", "BOS", DOWN, "above"),
          ("swing_bull_choch", "CHoCH", UP, "below"), ("swing_bear_choch", "CHoCH", DOWN, "above")]
    for col, txt, color, pos in ev:
        for i in np.where(ctx[col].to_numpy(bool))[0]:
            markers.append(dict(time=int(t_ep[i]), pos=pos, shape="circle", color=color, text=txt, kind="structure"))
    for col, txt, color, pos in (("int_bull_bos", "iBOS", "#80cbc4", "below"), ("int_bear_bos", "iBOS", "#ef9a9a", "above"),
                                 ("int_bull_choch", "iCHoCH", "#80cbc4", "below"), ("int_bear_choch", "iCHoCH", "#ef9a9a", "above")):
        for i in np.where(ctx[col].to_numpy(bool))[0]:
            markers.append(dict(time=int(t_ep[i]), pos=pos, shape="circle", color=color, text=txt, kind="internal"))
    for col, txt, pos in (("eqh", "EQH", "above"), ("eql", "EQL", "below")):
        for i in np.where(ctx[col].to_numpy(bool))[0]:
            markers.append(dict(time=int(t_ep[i]), pos=pos, shape="square", color=AMBER, text=txt, kind="eq"))
    sig = ctx["signal_event"].to_numpy()
    for i in np.where(np.isin(sig, ["long", "short"]))[0]:
        long = sig[i] == "long"
        markers.append(dict(time=int(t_ep[i]), pos="below" if long else "above", shape="arrowUp" if long else "arrowDown",
                            color=UP if long else DOWN, text="Signal ↑" if long else "Signal ↓", kind="signals"))
    return dict(zones=zones, segs=segs, markers=markers)


def strategy_fills(df: pd.DataFrame, cfg=None) -> list[dict]:
    """The Pine strategy's order fills, as TradingView draws them for `strategy()` scripts.

    Entries (Long = blue arrow up, Short = red arrow down), closes in purple when RSI reaches the 70/30 limits or the
    EMA trend flips (`strategy.close`), and reversals
    (an opposite entry that closes the open position on the same bar). Orders fill at the NEXT bar's open,
    so each marker sits on the fill bar. Computed on the timeframe's own bars, like TradingView.
    """
    from src.smc_logic.config import SMCConfig
    from src.smc_logic.triple_confirmation import simulate_strategy, triple_confirmation_frame

    if df is None or len(df) < 60:
        return []
    cfg = cfg or SMCConfig()
    f = triple_confirmation_frame(df, cfg)
    tr = simulate_strategy(df, cfg)
    if tr.empty:
        return []
    ts = pd.to_datetime(df["timestamp"]).reset_index(drop=True)
    pos_of = {t: i for i, t in enumerate(ts)}
    rsi, bear, bull = f["rsi"].to_numpy(float), f["trend_bear"].to_numpy(bool), f["trend_bull"].to_numpy(bool)
    entries = {(pd.Timestamp(r.entry_time), r.direction) for r in tr.itertuples(index=False)}
    exits = {(pd.Timestamp(r.exit_time), r.direction) for r in tr.itertuples(index=False) if pd.notna(r.exit_time)}
    out = []
    equity, prev_qty = float(INITIAL_CAPITAL), 0  # TradingView: default_qty_type=percent_of_equity, value 10
    for r in tr.itertuples(index=False):
        long = r.direction == "long"
        et = pd.Timestamp(r.entry_time)
        opposite = "short" if long else "long"
        rev_from = opposite if (et, opposite) in exits else None
        qty = max(int(math.floor(equity * QTY_PCT / float(r.entry_price))), 0)
        shown_qty = qty + (prev_qty if rev_from else 0)  # a reversal closes the old position AND opens the new one
        text = (f"Long +{shown_qty}" if long else f"Short -{shown_qty}") + (" · reverse" if rev_from else "")
        out.append(dict(time=_epoch(et), pos="below" if long else "above", shape="arrowUp" if long else "arrowDown",
                        color=LONG_BLUE if long else SHORT_RED, text=text, kind="fills"))
        if pd.isna(r.exit_time):
            continue
        if pd.notna(r.exit_price):
            pnl_here = (float(r.exit_price) - float(r.entry_price)) * qty * (1 if long else -1)
        xt = pd.Timestamp(r.exit_time)
        if (xt, opposite) in entries:
            equity += pnl_here  # reversal: no separate marker, but the P&L is realised
            prev_qty = qty
            continue  # shown as the reversing entry above instead of two stacked arrows
        i = pos_of.get(xt)
        sb = i - 1 if i else None
        if sb is None or sb < 0:
            why = "exit"
        elif long:
            why = f"RSI ≥ {cfg.rsi_overbought:g}" if rsi[sb] >= cfg.rsi_overbought else ("trend flip" if bear[sb] else "exit")
        else:
            why = f"RSI ≤ {cfg.rsi_oversold:g}" if rsi[sb] <= cfg.rsi_oversold else ("trend flip" if bull[sb] else "exit")
        out.append(dict(time=_epoch(xt), pos="above" if long else "below", shape="arrowDown" if long else "arrowUp",
                        color=PURPLE, text=f"Close {'Long' if long else 'Short'} {'-' if long else '+'}{qty} · {why}", kind="fills"))
        if pd.notna(r.exit_price):  # realised P&L feeds the next order's size, like TradingView's equity
            equity += (float(r.exit_price) - float(r.entry_price)) * qty * (1 if long else -1)
        prev_qty = qty
    return out



# ------------------------------------------------------- PDH / PDL / PWH / PWL
LEVEL_COLORS = {"PDH": "#f5b041", "PDL": "#f5b041", "PWH": "#9575cd", "PWL": "#9575cd"}


def prior_levels(src: pd.DataFrame, shown: pd.DataFrame, native: str) -> list[dict]:
    """Previous-day / previous-week high & low (regular session) drawn across each current day / week.

    Levels come from the native bars (`src`) and are laid over `shown`'s bars: the previous day's high/low across each
    day, the previous week's across each week. The newest segment extends to the right edge.
    """
    if src is None or len(src) == 0 or shown is None or len(shown) == 0 or native == "1Day":
        return []
    ts = pd.to_datetime(src["timestamp"])
    et = ts.dt.tz_localize("UTC").dt.tz_convert("America/New_York")
    rth = regular_hours_mask(src["timestamp"], native if native in ("5Min", "15Min") else "15Min").to_numpy()
    r = src[rth].assign(d=et[rth].dt.date.to_numpy())
    if r.empty:
        return []
    daily = r.groupby("d").agg(hi=("high", "max"), lo=("low", "min"))
    daily.index = pd.to_datetime(daily.index)
    weekly = daily.groupby(daily.index.to_period("W")).agg(hi=("hi", "max"), lo=("lo", "min"))

    sh_et = pd.to_datetime(shown["timestamp"]).dt.tz_localize("UTC").dt.tz_convert("America/New_York")
    sh = pd.DataFrame({"t": [_epoch(x) for x in shown["timestamp"]], "d": pd.to_datetime(sh_et.dt.date.to_numpy())})
    sh["w"] = sh["d"].dt.to_period("W")
    out: list[dict] = []
    days = sh.groupby("d", sort=True)
    last_day = sh["d"].max()
    for d, g in days:
        prev = daily[daily.index < d]
        if prev.empty:
            continue
        for name, val in (("PDH", prev["hi"].iat[-1]), ("PDL", prev["lo"].iat[-1])):
            out.append(dict(t0=int(g["t"].iat[0]), t1=int(g["t"].iat[-1]), p=round(float(val), 4), color=LEVEL_COLORS[name],
                            label=name, dash=True, kind="levels", lab=bool(d == last_day), live=bool(d == last_day)))
    last_week = sh["w"].max()
    for w, g in sh.groupby("w", sort=True):
        prev = weekly[weekly.index < w]
        if prev.empty:
            continue
        for name, val in (("PWH", prev["hi"].iat[-1]), ("PWL", prev["lo"].iat[-1])):
            out.append(dict(t0=int(g["t"].iat[0]), t1=int(g["t"].iat[-1]), p=round(float(val), 4), color=LEVEL_COLORS[name],
                            label=name, dash=True, kind="levels", lab=bool(w == last_week), live=bool(w == last_week)))
    return out


def trade_overlays(trades: pd.DataFrame, symbol: str, t_last: int) -> dict:
    """Entry/exit markers and SL/TP/entry lines for this symbol's trades."""
    markers, segs = [], []
    if trades is None or trades.empty:
        return dict(markers=markers, segs=segs)
    for t in trades[trades["symbol"] == symbol].itertuples(index=False):
        if pd.isna(t.entry_time) or t.entry_price is None or pd.isna(t.entry_price):
            continue
        te = _epoch(t.entry_time)
        tx = _epoch(t.exit_time) if pd.notna(t.exit_time) else t_last
        long = t.direction == "long"
        markers.append(dict(time=te, pos="below" if long else "above", shape="arrowUp" if long else "arrowDown",
                            color="#6c8cff", text=f"{'L' if long else 'S'} {t.qty:g} @ {t.entry_price:.2f}", kind="trades"))
        if pd.notna(t.exit_time) and t.exit_price is not None and not pd.isna(t.exit_price):
            win = (t.pnl or 0) > 0
            markers.append(dict(time=tx, pos="above" if long else "below", shape="square", color=UP if win else DOWN,
                                text=f"Exit {t.pnl:+.0f}" if pd.notna(t.pnl) else "Exit", kind="trades"))
        segs.append(dict(t0=te, t1=tx, p=round(float(t.entry_price), 4), color="#6c8cff", label="Entry", dash=False, kind="trade"))
        if t.stop_loss is not None and pd.notna(t.stop_loss):
            segs.append(dict(t0=te, t1=tx, p=round(float(t.stop_loss), 4), color=DOWN, label="SL", dash=True, kind="trade"))
        if t.take_profit is not None and pd.notna(t.take_profit):
            segs.append(dict(t0=te, t1=tx, p=round(float(t.take_profit), 4), color=UP, label="TP", dash=True, kind="trade"))
    return dict(markers=markers, segs=segs)


def _tf_dataset(src: pd.DataFrame, native: str, ctx: pd.DataFrame | None = None) -> dict:
    """One timeframe: candles/volume/EMA/RSI plus SMC overlays computed on THIS timeframe's own bars.

    (Signals and zones must be derived per timeframe, exactly as the Pine script does on a TradingView chart of
    that timeframe; drawing 15-minute signals on an hourly chart is not comparable to TradingView.)
    """
    from src.smc_logic.pipeline import compute_context

    full = resample_bars(src, native)
    if ctx is None and len(full) >= MIN_CTX_BARS:
        ctx = compute_context(full)
    keep = DISPLAY_BARS.get(native)
    shown = full.tail(keep).reset_index(drop=True) if keep else full
    ds = dataset_for(shown)
    ov = smc_overlays(ctx) if ctx is not None else dict(zones=[], segs=[], markers=[])
    ov["markers"] = ov["markers"] + strategy_fills(full)
    ov["segs"] = ov["segs"] + prior_levels(src, shown, native)
    if len(shown):
        t0 = _epoch(shown["timestamp"].iat[0])
        ov = dict(
            zones=[z for z in ov["zones"] if z["t1"] >= t0],
            segs=[g for g in ov["segs"] if g["t1"] >= t0],
            markers=[m for m in ov["markers"] if m["time"] >= t0],
        )
    return {**ds, **ov, "bars": int(len(shown))}


def build_chart_payload(bars: pd.DataFrame, ctx: pd.DataFrame | None, trades: pd.DataFrame, symbol: str,
                        default_tf: str = "15m", bars_5m: pd.DataFrame | None = None) -> dict:
    """Everything the browser chart needs, as plain JSON-serialisable data.

    `bars` are native 15-minute bars (30m/1H/2H/4H/1D are built from them); `bars_5m` are native 5-minute bars.
    `ctx` optionally reuses an already computed 15-minute context.
    """
    sets = {}
    for label, native in TIMEFRAMES.items():
        if native == "5Min":
            sets[label] = _tf_dataset(bars_5m if bars_5m is not None else bars.iloc[0:0], native)
        else:
            sets[label] = _tf_dataset(bars, native, ctx if native == "15Min" else None)
    t_last = _epoch(bars["timestamp"].iat[-1]) if len(bars) else 0
    return dict(symbol=symbol, default_tf=default_tf, datasets=sets, t_last=t_last, **_trade_part(trades, symbol, t_last))


def _trade_part(trades: pd.DataFrame, symbol: str, t_last: int) -> dict:
    tr = trade_overlays(trades, symbol, t_last)
    return dict(tsegs=tr["segs"], tmarkers=tr["markers"])


def with_trades(payload: dict, trades: pd.DataFrame) -> dict:
    """Attach (fresh) trade overlays to a cached trade-free payload."""
    return {**payload, **_trade_part(trades, payload["symbol"], payload["t_last"])}


# ------------------------------------------------------- watchlist / scanner
def _prev_close(bars: pd.DataFrame) -> float | None:
    if bars.empty:
        return None
    et = pd.to_datetime(bars["timestamp"]).dt.tz_localize("UTC").dt.tz_convert("America/New_York")
    rth = regular_hours_mask(bars["timestamp"], "15Min")
    today = et.iat[-1].date()
    prev = bars[(et.dt.date < today) & rth]
    return float(prev["close"].iat[-1]) if len(prev) else None


def scan_symbol(symbol: str, bars: pd.DataFrame, ctx: pd.DataFrame | None, sentiment: dict | None = None) -> dict:
    """One watchlist/scanner row: price, change, signal state and an explainable confluence score."""
    if bars.empty:
        return dict(symbol=symbol, price=None)
    price = float(bars["close"].iat[-1])
    pc = _prev_close(bars)
    row = dict(symbol=symbol, price=price, chg_pct=((price / pc - 1) * 100) if pc else None,
               spark=[round(float(x), 4) for x in bars["close"].tail(60)],
               sentiment=(sentiment or {}).get("score"), news_n=(sentiment or {}).get("n", 0))
    if ctx is None or len(ctx) < 50:
        return {**row, "signal": "—", "bias": None, "confluence": None}
    r = ctx.iloc[-1]
    long_bias = bool(r["ema_fast"] > r["ema_slow"])
    sig_idx = np.where(np.isin(ctx["signal_event"].to_numpy(), ["long", "short"]))[0]
    last_sig = ctx["signal_event"].iat[sig_idx[-1]] if len(sig_idx) else "—"
    age = int(len(ctx) - 1 - sig_idx[-1]) if len(sig_idx) else None
    checks = {
        "Trend (EMA9 vs 21)": True,
        "Momentum (RSI band)": bool(r["mom_bull"] if long_bias else r["mom_bear"]),
        "Volume spike": bool(r["vol_spike"]),
        "Zone (discount/premium)": str(r["zone"]).startswith("below") if long_bias else str(r["zone"]).startswith("above"),
        "Structure aligned": int(r["last_swing_dir"]) == (1 if long_bias else -1),
        "At order block": bool(r["in_bull_ob"] if long_bias else r["in_bear_ob"]),
    }
    return {**row, "signal": last_sig, "signal_age": age, "bias": "long" if long_bias else "short",
            "rsi": _f(r["rsi"], 1), "vol_ratio": _f(r["vol_ratio"], 2), "zone": str(r["zone"]),
            "structure": ("Bull" if int(r["swing_trend"]) > 0 else "Bear"), "checks": checks,
            "confluence": int(sum(checks.values())), "confluence_max": len(checks)}


# ---------------------------------------------------------------------- news
def load_news_feed(engine, limit: int = 80, symbols: list[str] | None = None, hours: int = 72) -> pd.DataFrame:
    since = pd.Timestamp.now(tz="UTC").tz_localize(None) - pd.Timedelta(hours=hours)
    with session_scope(engine) as s:
        q = (select(News, SentimentScore.score, SentimentScore.label)
             .outerjoin(SentimentScore, SentimentScore.news_id == News.id)
             .where(News.timestamp >= since).order_by(News.timestamp.desc()).limit(limit))
        if symbols:
            q = q.where(News.symbol.in_(symbols))
        rows = [dict(timestamp=n.timestamp, symbol=n.symbol, headline=n.headline, source=n.source, url=n.url,
                     score=sc, label=lb) for n, sc, lb in s.execute(q).all()]
    return pd.DataFrame(rows, columns=["timestamp", "symbol", "headline", "source", "url", "score", "label"])


# ----------------------------------------------------------------- model card
def load_model_card(models_dir: Path | None = None) -> dict | None:
    d = models_dir or (PROJECT_ROOT / "models")
    files = sorted(d.glob("smc_filter_*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    card = json.loads(files[-1].read_text())
    card["file"] = files[-1].name
    log = d / "MODEL_LOG.md"
    card["log"] = log.read_text()[-6000:] if log.exists() else ""
    return card


def latest_news_time(engine, symbol: str):
    """Timestamp (naive UTC) of the newest stored headline for `symbol`, or None."""
    from sqlalchemy import func

    with session_scope(engine) as s:
        return s.execute(select(func.max(News.timestamp)).where(News.symbol == symbol)).scalar()
