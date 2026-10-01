"""
Streamlit dashboard (read-only view of the SQLite DB).

    streamlit run src/dashboard/app.py
    DATABASE_URL=sqlite:///data/demo.db streamlit run src/dashboard/app.py   # offline demo data

Sections: KPI cards, equity curve (paper/live shading), P&L histogram, win rate by signal type and
by sentiment bucket, candlestick with entry/exit + SMC overlays, sortable trade log, system health.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # so `streamlit run` finds `src`

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from src.config import get_settings
from src.dashboard import metrics as m
from src.data_ingestion.backfill import latest_bar_time, load_bars
from src.db.schema import get_engine, init_db

st.set_page_config(page_title="AI Trading Platform", page_icon="📈", layout="wide")


@st.cache_resource
def _engine():
    e = get_engine()
    init_db(e)
    return e


@st.cache_data(ttl=30)
def _trades():
    return m.load_trades(_engine())


@st.cache_data(ttl=60)
def _bars(symbol: str, timeframe: str, days: int):
    since = m.utcnow() - pd.Timedelta(days=days)
    return load_bars(_engine(), symbol, timeframe, since=since)


@st.cache_data(ttl=120)
def _context(symbol: str, timeframe: str):
    from src.smc_logic.pipeline import compute_context

    bars = load_bars(_engine(), symbol, timeframe, since=m.utcnow() - pd.Timedelta(days=60))
    return compute_context(bars) if len(bars) >= 250 else None


def money(x: float | None) -> str:
    return "—" if x is None else f"{'-' if x < 0 else ''}${abs(x):,.2f}"


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def num(x: float | None, d: int = 2) -> str:
    return "—" if x is None else f"{x:.{d}f}"


s = get_settings()
engine = _engine()

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.title("📈 AI Trading Platform")
    start_equity = st.number_input("Starting equity ($)", value=100_000.0, step=1_000.0)
    mode_filter = st.radio("Trades", ["All", "paper", "live"], horizontal=True)
    symbols = st.multiselect("Symbols", s.tickers, default=[])
    if st.button("Refresh now"):
        st.cache_data.clear()
    st.caption(f"DB: `{s.database_url.split('///')[-1]}`  ·  times in America/Chicago")

trades = _trades()
if mode_filter != "All" and not trades.empty:
    trades = trades[trades["mode"] == mode_filter]
if symbols and not trades.empty:
    trades = trades[trades["symbol"].isin(symbols)]

prob, model_ver, prob_ts = m.latest_model_probability(engine)
k = m.kpis(trades, start_equity, model_prob=prob)

mode_badge = "🔴 LIVE" if s.is_live else "🟢 PAPER"
st.header(f"Dashboard  {mode_badge}")

if trades.empty:
    st.info("No trades yet. Once the scheduler runs (or you load demo data with "
            "`python -m src.backtest.replay`), results appear here.")

# ----------------------------------------------------------------- KPI cards
c = st.columns(5)
c[0].metric("Net P&L today", money(k["pnl_today"]))
c[1].metric("This week", money(k["pnl_week"]))
c[2].metric("This month", money(k["pnl_month"]))
c[3].metric("All-time", money(k["pnl_all"]))
c[4].metric("Open exposure", money(k["open_exposure"]))
c = st.columns(6)
c[0].metric("Win rate", pct(k["win_rate"]), f"{k['wins']}W / {k['losses']}L", delta_color="off")
c[1].metric("Avg win / avg loss", num(k["win_loss_ratio"]))
c[2].metric("Profit factor", num(k["profit_factor"]))
c[3].metric("Closed trades", k["trades"])
c[4].metric("Sharpe (daily)", num(k["sharpe"]))
c[5].metric("Max drawdown", pct(abs(k["max_drawdown"]) if k["max_drawdown"] else 0.0))
c = st.columns(2)
c[0].metric("Latest model confidence", pct(prob) if prob is not None else "no model yet",
            help=f"model {model_ver}" if model_ver else "Train with `python -m src.ml.train`")
if k["trades"] and k["trades"] < 30:
    c[1].info(f"Only {k['trades']} closed trades — treat every ratio above as noise until n ≳ 30–50.")

tab_perf, tab_chart, tab_log, tab_health = st.tabs(["Performance", "Chart", "Trade log", "System health"])

# --------------------------------------------------------------- performance
with tab_perf:
    eq = m.equity_curve(trades, start_equity)
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Equity curve")
        if eq.empty:
            st.caption("No closed trades yet.")
        else:
            fig = go.Figure()
            t_local = m.to_local(eq["time"])
            # one coloured segment per mode so paper/live runs are visually separate
            for mode, color in (("paper", "#2E86DE"), ("live", "#E67E22")):
                seg = eq["mode"] == mode
                if seg.any():
                    fig.add_trace(go.Scatter(x=t_local[seg], y=eq["equity"][seg], mode="lines+markers",
                                             name=mode, line=dict(color=color, width=2), marker=dict(size=4)))
            fig.add_hline(y=start_equity, line_dash="dot", line_color="gray")
            fig.update_layout(height=360, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="Equity ($)")
            st.plotly_chart(fig, width="stretch")
    with right:
        st.subheader("P&L per trade")
        cl = m.closed(trades)
        if cl.empty:
            st.caption("No closed trades yet.")
        else:
            fig = go.Figure(go.Histogram(x=cl["pnl"], nbinsx=25, marker_color="#7F8C8D"))
            fig.add_vline(x=0, line_color="black")
            fig.update_layout(height=360, margin=dict(l=10, r=10, t=10, b=10), xaxis_title="P&L ($)", yaxis_title="Trades")
            st.plotly_chart(fig, width="stretch")

    a, b = st.columns(2)
    for col, title, df in ((a, "Win rate by signal type", m.winrate_by_signal(trades)),
                           (b, "Win rate by sentiment at entry", m.winrate_by_sentiment(trades))):
        with col:
            st.subheader(title)
            if df.empty:
                st.caption("Not enough data yet.")
                continue
            fig = go.Figure(go.Bar(x=df["group"], y=df["win_rate"] * 100, text=[f"n={int(n)}" for n in df["trades"]],
                                   marker_color="#27AE60"))
            fig.add_hline(y=50, line_dash="dot", line_color="gray")
            fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="Win rate (%)", yaxis_range=[0, 100])
            st.plotly_chart(fig, width="stretch")

    rs = m.rolling_sharpe(trades, start_equity)
    if len(rs.dropna()) >= 10:
        st.subheader("Rolling Sharpe (20 trading days)")
        st.line_chart(rs.dropna())

# --------------------------------------------------------------------- chart
with tab_chart:
    cc = st.columns([1, 1, 1, 1])
    sym = cc[0].selectbox("Symbol", s.tickers)
    days = cc[1].slider("Days", 2, 30, 8)
    show_smc = cc[2].checkbox("SMC overlays", True)
    show_ema = cc[3].checkbox("EMA 9/21", True)
    bars = _bars(sym, s.timeframe, days)
    if bars.empty:
        st.info(f"No bars stored for {sym}. Run `python -m src.data_ingestion.backfill`.")
    else:
        ctx = _context(sym, s.timeframe) if show_smc or show_ema else None
        if ctx is not None:
            ctx = ctx[ctx["timestamp"] >= bars["timestamp"].iat[0]].reset_index(drop=True)
        x = m.to_local(bars["timestamp"])
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.8, 0.2], vertical_spacing=0.02)
        fig.add_trace(go.Candlestick(x=x, open=bars["open"], high=bars["high"], low=bars["low"], close=bars["close"],
                                     name=sym, increasing_line_color="#26A69A", decreasing_line_color="#EF5350"), row=1, col=1)
        fig.add_trace(go.Bar(x=x, y=bars["volume"], marker_color="#B0BEC5", name="Volume"), row=2, col=1)
        if ctx is not None and len(ctx):
            cx = m.to_local(ctx["timestamp"])
            if show_ema:
                fig.add_trace(go.Scatter(x=cx, y=ctx["ema_fast"], name="EMA9", line=dict(width=1, color="#F39C12")), row=1, col=1)
                fig.add_trace(go.Scatter(x=cx, y=ctx["ema_slow"], name="EMA21", line=dict(width=1, color="#8E44AD")), row=1, col=1)
            if show_smc:
                for hi, lo, color, nm in (("bull_ob_high", "bull_ob_low", "rgba(38,166,154,0.25)", "Bull OB"),
                                          ("bear_ob_high", "bear_ob_low", "rgba(239,83,80,0.25)", "Bear OB")):
                    h, l = ctx[hi], ctx[lo]
                    fig.add_trace(go.Scatter(x=cx, y=h, mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip"), row=1, col=1)
                    fig.add_trace(go.Scatter(x=cx, y=l, mode="lines", line=dict(width=0), fill="tonexty",
                                             fillcolor=color, name=nm, hoverinfo="skip"), row=1, col=1)
                for col_, color, dash in (("swing_high", "#C0392B", "dot"), ("swing_low", "#16A085", "dot")):
                    fig.add_trace(go.Scatter(x=cx, y=ctx[col_], mode="lines", line=dict(width=1, color=color, dash=dash),
                                             name=col_.replace("_", " ")), row=1, col=1)
                for col_, sym_, color, nm in (("swing_bull_bos", "triangle-up", "#16A085", "BOS ↑"), ("swing_bear_bos", "triangle-down", "#C0392B", "BOS ↓"),
                                              ("swing_bull_choch", "star", "#16A085", "CHoCH ↑"), ("swing_bear_choch", "star", "#C0392B", "CHoCH ↓")):
                    msk = ctx[col_].astype(bool)
                    if msk.any():
                        yy = ctx["low"][msk] * 0.999 if "bull" in col_ else ctx["high"][msk] * 1.001
                        fig.add_trace(go.Scatter(x=cx[msk], y=yy, mode="markers", name=nm,
                                                 marker=dict(symbol=sym_, size=9, color=color)), row=1, col=1)
        tr = m.closed(trades[trades["symbol"] == sym]) if not trades.empty else trades
        allsym = trades[trades["symbol"] == sym] if not trades.empty else trades
        if len(allsym):
            ent = allsym[allsym["entry_time"].notna() & (allsym["entry_time"] >= bars["timestamp"].iat[0])]
            if len(ent):
                fig.add_trace(go.Scatter(x=m.to_local(ent["entry_time"]), y=ent["entry_price"], mode="markers", name="Entry",
                                         marker=dict(symbol=["triangle-up" if d == "long" else "triangle-down" for d in ent["direction"]],
                                                     size=13, color="#1565C0", line=dict(width=1, color="white")),
                                         text=[f"{d} {q:g} @ {p:.2f}" for d, q, p in zip(ent["direction"], ent["qty"], ent["entry_price"])]), row=1, col=1)
            ex = allsym[allsym["exit_time"].notna() & allsym["exit_price"].notna() & (allsym["exit_time"] >= bars["timestamp"].iat[0])]
            if len(ex):
                fig.add_trace(go.Scatter(x=m.to_local(ex["exit_time"]), y=ex["exit_price"], mode="markers", name="Exit",
                                         marker=dict(symbol="x", size=11, color=["#2E7D32" if (p or 0) > 0 else "#C62828" for p in ex["pnl"]]),
                                         text=[f"P&L {p:+.2f}" if pd.notna(p) else "" for p in ex["pnl"]]), row=1, col=1)
        fig.update_layout(height=640, margin=dict(l=10, r=10, t=10, b=10), xaxis_rangeslider_visible=False,
                          legend=dict(orientation="h", y=1.04))
        # hide non-trading gaps (overnight/weekends) so candles are contiguous
        fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"]), dict(bounds=[15.25, 8.5], pattern="hour")])
        st.plotly_chart(fig, width="stretch")

# ----------------------------------------------------------------- trade log
with tab_log:
    if trades.empty:
        st.caption("No trades yet.")
    else:
        view = trades.copy()
        view["entry (CT)"] = m.to_local(view["entry_time"]).dt.strftime("%Y-%m-%d %H:%M") if view["entry_time"].notna().any() else None
        view["exit (CT)"] = m.to_local(view["exit_time"].fillna(pd.NaT)).dt.strftime("%Y-%m-%d %H:%M") if view["exit_time"].notna().any() else None
        status = st.multiselect("Status", sorted(view["status"].unique()), default=sorted(view["status"].unique()))
        view = view[view["status"].isin(status)]
        cols = ["id", "symbol", "direction", "entry (CT)", "exit (CT)", "entry_price", "exit_price", "qty", "pnl",
                "model_probability", "sentiment_at_entry", "stop_loss", "take_profit", "mode", "status", "note"]
        st.dataframe(view.sort_values("id", ascending=False)[cols], width="stretch", hide_index=True,
                     column_config={"pnl": st.column_config.NumberColumn("P&L ($)", format="%.2f"),
                                    "model_probability": st.column_config.NumberColumn("P(win)", format="%.2f")})
        st.download_button("Download CSV", view[cols].to_csv(index=False), "trades.csv", "text/csv")

# -------------------------------------------------------------------- health
with tab_health:
    ref_sym = s.tickers[0]
    latest = max((t for t in (latest_bar_time(engine, x, s.timeframe) for x in s.tickers) if t), default=None)
    h = m.health(engine, latest, trading_mode=s.trading_mode)
    icon = "✅" if h["status"] == "ok" else "⚠️"
    st.subheader(f"{icon} System health")
    c = st.columns(4)
    c[0].metric("Mode", h["mode"].upper())
    c[1].metric("Last scheduler cycle", "never" if h["last_cycle"] is None else f"{h['minutes_since_cycle']:.0f} min ago")
    c[2].metric("API/cycle errors (24h)", h["errors_24h"])
    c[3].metric("Newest bar age", "—" if h["data_stale_minutes"] is None else f"{h['data_stale_minutes']:.0f} min")
    st.caption(
        ("Market window is OPEN (08:30–15:00 CT)." if h["in_trading_window"]
         else f"Market window closed. Next session opens {h['next_open']:%a %b %d %H:%M %Z}." if h["next_open"] is not None
         else "Market window closed.")
        + ("  ·  🛑 KILL SWITCH ACTIVE — no new orders" if s.kill_switch_active else "")
    )
    ev = m.load_events(engine, hours=72)
    if ev.empty:
        st.caption("No system events yet.")
    else:
        kinds = st.multiselect("Event types", sorted(ev["kind"].unique()), default=[x for x in sorted(ev["kind"].unique()) if x != "cycle"])
        ev = ev[ev["kind"].isin(kinds)].copy()
        ev["time (CT)"] = m.to_local(ev["timestamp"]).dt.strftime("%m-%d %H:%M:%S")
        st.dataframe(ev[["time (CT)", "kind", "message"]].head(300), width="stretch", hide_index=True)
