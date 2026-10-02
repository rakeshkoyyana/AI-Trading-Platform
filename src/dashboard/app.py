"""
AlphaWave dashboard (Streamlit).

    streamlit run src/dashboard/app.py
    DATABASE_URL=sqlite:///data/demo.db streamlit run src/dashboard/app.py     # offline demo data

Layout: top bar + scrolling price/news tape, KPI cards, a TradingView-style interactive chart with
SMC overlays and drawing tools, a right rail (watchlist, live news, decision feed) and tabs for the
scanner, trades, performance, decision log, ML model card, system health and settings.
The dashboard never talks to the broker. It writes only the KILL_SWITCH file, each ticker's trade mode (Off / Ask / Auto)
and your Approve / Reject decisions; the scheduler process is what actually places orders.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # so `streamlit run` finds `src`

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from src.config import get_settings
from src import universe as U
from src.dashboard import alerts_ui
from src.dashboard import brand
from src.dashboard import data as D
from src.dashboard import metrics as m
from src.dashboard import theme as T
from src.dashboard.chart_component import chart_html
from src.data_ingestion.backfill import latest_bar_time, load_bars
from src.data_ingestion.on_demand import ensure_symbol_data
from src.decision_engine import council
from src.execution import control
from src.db.schema import get_engine, init_db

st.set_page_config(page_title=brand.NAME, page_icon=brand.page_icon(), layout="wide", initial_sidebar_state="collapsed")
st.html(T.CSS)

S = get_settings()


@st.cache_resource
def _engine():
    e = get_engine()
    init_db(e)
    return e


engine = _engine()
DB = S.database_url


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _render_html(html: str, height: int) -> None:
    """Embed a self-contained HTML page (st.iframe on current Streamlit, components.html on older)."""
    if hasattr(st, "iframe"):
        st.iframe(html, height=height)
    else:  # pragma: no cover - older Streamlit
        components.html(html, height=height, scrolling=False)


# ------------------------------------------------------------------ cached loaders
@st.cache_data(ttl=45)
def _trades(db: str, sig: object = None) -> pd.DataFrame:  # `sig` busts the cache the moment trades/orders change
    return m.load_trades(engine)


@st.cache_data(ttl=60)
def _bars(db: str, sym: str, days: int, ext: bool, last_ts: str) -> pd.DataFrame:
    return load_bars(engine, sym, S.timeframe, since=utc_now() - timedelta(days=days), extended_hours=ext)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _assets() -> list[dict]:
    return U.load_assets()


@st.cache_data(ttl=120, show_spinner=False)
def _ensure(sym: str) -> dict:
    """Pull a symbol's bars the first time it is opened (then only top up the newest ones)."""
    return ensure_symbol_data(sym, engine=engine)


@st.cache_data(ttl=120)
def _ctx(db: str, sym: str, ext: bool, last_ts: str):
    from src.smc_logic.pipeline import compute_context

    bars = _bars(db, sym, 60, ext, last_ts)
    return compute_context(bars) if len(bars) >= 250 else None


@st.cache_data(ttl=60)
def _news(db: str, limit: int, hours: int) -> pd.DataFrame:
    return D.load_news_feed(engine, limit=limit, hours=hours)


@st.cache_data(ttl=60)
def _events(db: str, hours: int) -> pd.DataFrame:
    return m.load_events(engine, hours=hours)


def _last_ts(sym: str, tf: str | None = None) -> str:
    t = latest_bar_time(engine, sym, tf or S.timeframe)
    return str(t) if t else "none"


@st.cache_data(ttl=45)
def _chart_core(db: str, sym: str, ext: bool, last15: str, last5: str) -> dict:
    """Chart datasets with SMC overlays computed per timeframe (trade overlays are attached fresh, uncached)."""
    b15 = load_bars(engine, sym, "15Min", since=utc_now() - timedelta(days=400), extended_hours=ext)
    b5 = load_bars(engine, sym, "5Min", since=utc_now() - timedelta(days=45), extended_hours=ext)
    if S.live_hybrid:  # newest minutes from the real-time IEX feed (estimate, never stored); exact SIP replaces it later
        from src.data_ingestion.live_tail import with_live_tail

        now = datetime.now(timezone.utc)
        if len(b15):
            b15, _ = with_live_tail(b15, sym, "15Min", now=now, regular_only=not ext)
        if len(b5):
            b5, _ = with_live_tail(b5, sym, "5Min", now=now, regular_only=not ext)
    return D.build_chart_payload(b15, None, pd.DataFrame(columns=["symbol"]), sym, bars_5m=b5)


def _sentiment(sym: str) -> dict:
    try:
        from src.sentiment.aggregate import get_rolling_sentiment

        return get_rolling_sentiment(sym, window_hours=24.0, engine=engine)
    except Exception:  # noqa: BLE001
        return {"score": None, "n": 0}


@st.cache_data(ttl=60)
def _scan_rows(db: str, tickers: tuple, ext: bool, stamp: str) -> list[dict]:
    rows = []
    for sym in tickers:
        lt = _last_ts(sym)
        bars = _bars(db, sym, 60, ext, lt)
        rows.append(D.scan_symbol(sym, bars, _ctx(db, sym, ext, lt), _sentiment(sym)))
    return rows


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.markdown("### Controls")
    start_equity = st.number_input("Starting equity ($)", value=100_000.0, step=1_000.0)
    refresh = st.selectbox("Auto-refresh", ["Off", "15s", "30s", "60s"], index=2)
    ext = st.toggle("Include extended-hours bars", value=S.include_extended_hours,
                    help="Match this to your TradingView chart's Extended hours setting.")
    alert_sound = st.toggle("Chime on new approval requests", value=True,
                            help="Plays a short chime when a trade needs your approval. Browsers only allow sound after you have clicked on the page once.")
    mode_filter = st.radio("Trades shown", ["All", "paper", "live"], horizontal=True)
    if st.button("Clear caches"):
        st.cache_data.clear()
    st.caption(f"DB `{DB.split('///')[-1]}`  ·  times in America/Chicago")

RUN_EVERY = None if refresh == "Off" else refresh
tickers = tuple(S.tickers)  # the trade list (TICKERS in .env): the only symbols the scheduler ever trades
watch = [w for w in U.load_watchlist() if w not in tickers]  # research-only symbols you opened and starred
scan_syms = tuple(tickers) + tuple(watch)
SIG = control.change_signature(engine)  # what trade control compares against, to redraw the page on any change
st.session_state["_sig"] = SIG
trades_all = _trades(DB, hash(SIG))
trades = trades_all if mode_filter == "All" or trades_all.empty else trades_all[trades_all["mode"] == mode_filter]
stamp = "|".join(_last_ts(t) for t in scan_syms)
rows = _scan_rows(DB, scan_syms, ext, stamp)
by_sym = {r["symbol"]: r for r in rows}
prob, model_ver, _ = m.latest_model_probability(engine)
k = m.kpis(trades, start_equity, model_prob=prob)


# ------------------------------------------------------------------ live header + tape
def _market_state() -> tuple[bool, str | None]:
    try:
        from src.scheduler.market_hours import is_trading_window_now, next_session_open

        now = utc_now()
        if is_trading_window_now(now, S):
            return True, None
        return False, next_session_open(now, S).strftime("%a %H:%M CT")
    except Exception:  # noqa: BLE001
        return False, None


@st.fragment(run_every=RUN_EVERY)
def header_and_tape():
    is_open, nxt = _market_state()
    local = pd.Timestamp(utc_now(), tz="UTC").tz_convert(m.TZ)
    st.html(T.topbar(S.is_live, is_open, nxt, S.kill_switch_active, start_equity + k["pnl_all"], local.strftime("%a %b %d  %H:%M:%S CT")))
    nf = _news(DB, 30, 48)
    news = [dict(symbol=r.symbol, headline=r.headline, score=r.score) for r in nf.head(16).itertuples()]
    st.html(T.ticker_tape(rows, news))


header_and_tape()

# ------------------------------------------------------------------ trade control
_MODE_HELP = ("**Off** – never open a position (an existing one is still closed by the Pine exit rules). "
              "**Ask** – a qualifying signal waits here for your Approve / Reject. "
              "**Auto** – a qualifying signal is sent to Alpaca immediately (every risk gate still applies).")


def _mode_changed(sym_: str) -> None:
    v = st.session_state.get(f"mode_{sym_}")
    if v:
        control.set_mode(engine, sym_, v.lower())


@st.fragment(run_every="3s")  # approvals are time-limited and closes should show up fast, so this always polls
def trade_control():
    if control.change_signature(engine) != st.session_state.get("_sig"):
        st.rerun()  # a trade / approval / close changed in the database: redraw the whole page, no manual refresh
    viewed = [x for x in (st.session_state.get("viewing"), st.session_state.get("sym")) if x]
    syms = list(dict.fromkeys([*tickers, *watch, *viewed]))
    modes = control.get_modes(engine, syms, S)
    syms = list(dict.fromkeys([*syms, *modes]))
    pend = control.list_pending(engine, "pending")
    n_active = sum(1 for v in modes.values() if v != "off")
    seen = st.session_state.setdefault("alerted_ids", set())
    fresh = alerts_ui.new_alert_ids(pend, seen)
    if pend:
        st.html(alerts_ui.banner_html(pend, utc_now()))
    if fresh:
        seen.update(fresh)
        if alert_sound:
            st.audio(alerts_ui.chime_wav(), format="audio/wav", autoplay=True)  # only on a NEW request
    with st.container(border=True):
        h1, h2 = st.columns([3, 2])
        h1.markdown(f"**Trade control** · {n_active} of {len(syms)} tickers active today")
        h2.caption("Only tickers set to Ask or Auto are scanned for entries.", help=_MODE_HELP)
        per_row = 4
        for i in range(0, len(syms), per_row):
            cols = st.columns(per_row)
            for col, s_ in zip(cols, syms[i:i + per_row]):
                with col:
                    st.caption(f"**{s_}**" + ("" if s_ in tickers else "  ·  ☆ research"))
                    st.segmented_control(f"Mode {s_}", ["Off", "Ask", "Auto"], default=modes[s_].title(), key=f"mode_{s_}",
                                         on_change=_mode_changed, args=(s_,), label_visibility="collapsed")
        if pend:
            st.markdown(f"**Waiting for your approval ({len(pend)})**")
        for p in pend:
            left_s = max(int((p.expires_at - utc_now()).total_seconds()), 0)
            prob_s = f" · P(win) {p.probability:.0%}" if p.probability is not None else ""
            stop_s = f"stop {p.stop_loss:.2f}" if p.stop_loss else "no stop"
            c1, c2, c3 = st.columns([4, 1, 1])
            c1.markdown(f"{'🟢' if p.direction == 'long' else '🔴'} **{p.direction.upper()} {p.symbol}** × {p.qty} @ ~{p.entry:.2f} · "
                        f"{stop_s}{prob_s} · expires in {left_s // 60}:{left_s % 60:02d}")
            if c2.button("Approve", key=f"ap_{p.id}", type="primary"):
                ok = control.decide(engine, p.id, True)
                st.toast("Approved – the scheduler will send it within ~2 s" if ok else "Too late – that request expired")
                st.rerun()
            if c3.button("Reject", key=f"rj_{p.id}"):
                control.decide(engine, p.id, False)
                st.rerun()
            cv = council.get_vote(engine, p.symbol, p.signal_time) if p.signal_time else None
            if cv is not None:
                icon = {"agree": "✅", "mixed": "➖", "disagree": "⚠️"}.get(cv.verdict, "")
                c1.caption(f"{icon} Council (shadow, advisory): **{cv.verdict}** ({cv.score:+.2f})")
            with c1.expander("Why this signal"):
                import json as _json

                for line in _json.loads(p.reasons_json or "[]"):
                    st.caption(line)
        held = control.open_positions(engine)
        waiting = {r.symbol for r in control.list_close_requests(engine, "pending")}
        if held:
            st.markdown(f"**Open positions ({len(held)})**")
        for h in held:
            e_s = f"@ {h['entry']:.2f}" if h["entry"] else ""
            st_s = f" · stop {h['stop']:.2f}" if h["stop"] else ""
            tp_s = f" · target {h['target']:.2f}" if h["target"] else ""
            c1, c2 = st.columns([5, 2])
            c1.markdown(f"{'🟢' if h['direction'] == 'long' else '🔴'} **{h['direction'].upper()} {h['symbol']}** × {h['qty']} {e_s}{st_s}{tp_s}")
            if h["symbol"] in waiting:
                c2.caption("closing… (sent within ~2 s)")
            elif st.session_state.get("confirm_close") == h["symbol"]:
                b1, b2 = c2.columns(2)
                if b1.button("Confirm", key=f"cc_{h['symbol']}", type="primary"):
                    ok = control.request_close(engine, h["symbol"])
                    st.session_state.pop("confirm_close", None)
                    st.toast("Closing at market – the scheduler sends it within ~2 s" if ok else "Already closing")
                    st.rerun()
                if b2.button("Cancel", key=f"cx_{h['symbol']}"):
                    st.session_state.pop("confirm_close", None)
                    st.rerun()
            elif c2.button(f"Close {h['symbol']}", key=f"cl_{h['symbol']}"):
                st.session_state["confirm_close"] = h["symbol"]
                st.rerun()
        recent = [r for r in control.list_pending(engine, None) if r.status in {"executed", "failed", "rejected"}][:4]
        gone = [r for r in control.list_close_requests(engine, None) if r.status in {"done", "failed", "expired"}][:2]
        for r in gone:
            st.caption(f"Manual close {r.symbol}: {r.status}" + (f" ({r.note})" if r.note else ""))
        if recent:
            st.caption("Recent: " + "  ·  ".join(
                f"{r.symbol} {r.direction} – {r.status}" + (f" ({r.note})" if r.status == "failed" and r.note else "") for r in recent))
        st.caption("Approved orders are sent by the scheduler (`python -m src.scheduler.run_loop`) within a couple of seconds; "
                   "open positions also close on the Pine exit rules, the take-profit or stop at the broker, and are flattened before the close.")


trade_control()

# ------------------------------------------------------------------ KPI cards
eq = m.equity_curve(trades, start_equity)
eq_spark = eq["equity"].tail(40).tolist() if not eq.empty else None
pnl_spark = m.closed(trades)["pnl"].cumsum().tail(40).tolist() if not trades.empty and k["trades"] else None
dd = abs(k["max_drawdown"]) * 100 if k["max_drawdown"] else 0.0
open_n = int(trades["status"].isin(["open", "filled"]).sum()) if not trades.empty else 0
cards = [
    T.kpi("Net P&L today", T.money(k["pnl_today"], True), f"week {T.money(k['pnl_week'], True)}", pnl_spark, T.UP if k["pnl_today"] >= 0 else T.DOWN, T.tone(k["pnl_today"])),
    T.kpi("Month / all-time", T.money(k["pnl_month"], True), f"all-time {T.money(k['pnl_all'], True)}", eq_spark, T.ACCENT, T.tone(k["pnl_month"])),
    T.kpi("Win rate", T.pct(k["win_rate"] * 100 if k["win_rate"] is not None else None), f"{k['wins']}W · {k['losses']}L · {k['trades']} trades"),
    T.kpi("Avg win / avg loss", f"{k['win_loss_ratio']:.2f}" if k["win_loss_ratio"] else "—", f"profit factor {k['profit_factor']:.2f}" if k["profit_factor"] else "profit factor —"),
    T.kpi("Sharpe (daily)", f"{k['sharpe']:.2f}" if k["sharpe"] is not None else "—", f"max drawdown {dd:.1f}%"),
    T.kpi("Open exposure", T.money(k["open_exposure"]), f"{open_n} open position(s)"),
    T.kpi("Model confidence", T.pct(prob * 100) if prob is not None else "no model", f"{model_ver}" if model_ver else "train: python -m src.ml.train"),
]
st.html('<div class="kpis">' + "".join(cards) + "</div>")
if 0 < k["trades"] < 30:
    st.html(f'<div class="banner warn">Only <b>{k["trades"]}</b> closed trades so far — treat win rate, Sharpe and ratios as noise until n ≳ 30–50.</div>')


# ------------------------------------------------------------------ main: chart + right rail
left, right = st.columns([3.35, 1.15], gap="small")

with left:
    assets = _assets()
    names = {a["symbol"]: a.get("name", "") for a in assets}

    def _picked():  # runs before the rerun, so the pills below can be pointed at the new symbol
        v = st.session_state.get("search")
        if v:
            st.session_state["viewing"] = v
            st.session_state["sym"] = v
            st.session_state["search"] = None  # clear the box, like a TradingView symbol search

    st.selectbox("Search", options=sorted(names), index=None, key="search", on_change=_picked, label_visibility="collapsed",
                 placeholder="Search any US stock or ETF — ticker or company name (data loads when you open it)",
                 format_func=lambda v: f"{v} — {names.get(v, '')}" if names.get(v) else v)
    opts = list(dict.fromkeys(list(tickers) + watch + [x for x in (st.session_state.get("viewing"), st.session_state.get("sym")) if x]))
    if st.session_state.get("sym") not in opts:
        st.session_state["sym"] = tickers[0]
    sym = st.pills("Symbol", opts, selection_mode="single", label_visibility="collapsed", key="sym",
                   format_func=lambda v: v if v in tickers else f"☆ {v}") or tickers[0]
    if sym not in tickers:
        c1, c2 = st.columns([1, 3])
        if sym in watch:
            if c1.button("★ Remove from watchlist", key="wl_rm"):
                U.remove_from_watchlist(sym)
                st.session_state.pop("viewing", None)
                st.rerun()
        elif c1.button("☆ Add to watchlist", key="wl_add"):
            _, err = U.add_to_watchlist(sym)
            st.toast(err or f"{sym} added to your watchlist")
            st.rerun()
        c2.caption(f"{names.get(sym) or sym} · research only until you set its mode to Ask or Auto in Trade control.")

    @st.fragment(run_every=("60s" if RUN_EVERY else None))
    def chart_panel():
        if sym not in tickers or _last_ts(sym, "15Min") == "none":
            with st.spinner(f"Loading {sym} from Alpaca ({'first open, about 10 seconds' if _last_ts(sym, '15Min') == 'none' else 'refreshing'})…"):
                info = _ensure(sym)
            if info.get("error"):
                st.warning(f"{sym}: {info['error']}")
        core = _chart_core(DB, sym, ext, _last_ts(sym, "15Min"), _last_ts(sym, "5Min"))
        if not core["t_last"]:
            st.info(f"No bars stored for {sym}. Run `python -m src.data_ingestion.backfill`.")
            return
        _render_html(chart_html(D.with_trades(core, trades_all)), 760)

    chart_panel()

    rt = trades.copy()
    if not rt.empty:
        rt["time"] = rt["exit_time"].fillna(rt["entry_time"])
        rt = rt.sort_values("time", ascending=False, na_position="last").head(8)
    st.html('<div class="panel"><h4>Recent trades <span class="mut" style="text-transform:none;letter-spacing:0">open positions first appear here as “open/filled”</span></h4>'
            f'{T.recent_trades(rt.to_dict("records") if not rt.empty else [], utc_now())}</div>')

with right:
    st.html('<div class="panel"><h4>Watchlist <span class="mut" style="text-transform:none;letter-spacing:0">dots = confluence</span></h4>'
            f'<div class="scroll" style="max-height:330px">{T.watchlist(rows, sym)}</div></div>')

    @st.fragment(run_every=RUN_EVERY)
    def news_panel():
        c1, c2 = st.columns([1.4, 1])
        scope = c1.selectbox("News", ["All", sym], label_visibility="collapsed", key="news_scope")
        if c2.button("Fetch latest", key="fetch_news", help="Pull Finnhub/NewsAPI + score with FinBERT (needs keys)"):
            with st.spinner("Fetching news…"):
                try:
                    from src.sentiment.aggregate import refresh_sentiment

                    refresh_sentiment(list(tickers), engine=engine)
                    st.cache_data.clear()
                except Exception as exc:  # noqa: BLE001
                    st.warning(f"News fetch failed: {exc}")
        nf = _news(DB, 80, 72)
        if scope != "All":
            nf = nf[nf["symbol"] == scope]
        items = nf.head(40).to_dict("records")
        st.html(f'<div class="panel"><h4>Live news <span class="mut" style="text-transform:none;letter-spacing:0">{len(nf)} items · FinBERT</span></h4>'
                f'<div class="scroll" style="max-height:360px">{T.news_list(items, utc_now())}</div></div>')

    news_panel()

    ev = _events(DB, 72)
    dec = ev[ev["kind"] == "decision"].head(25).to_dict("records") if not ev.empty else []
    st.html(f'<div class="panel"><h4>Decision feed <span class="mut" style="text-transform:none;letter-spacing:0">why we did / didn’t trade</span></h4>'
            f'<div class="scroll" style="max-height:250px">{T.decision_list(dec, utc_now())}</div></div>')

# ------------------------------------------------------------------ tabs
tab_scan, tab_trades, tab_perf, tab_dec, tab_council, tab_ml, tab_sys, tab_set = st.tabs(
    ["Scanner", "Trades", "Performance", "Decision log", "Council (shadow)", "ML model", "System", "Settings"])

# ---- Council (shadow)
with tab_council:
    st.caption("Free, rule-based analyst votes on every fresh signal (momentum room, volume, structure, premium/discount, order blocks/FVG, "
               "higher timeframes, sentiment). **Advisory only: it never changes a decision.** Judge it by whether agreement predicts "
               "wins; verdict thresholds are fixed in advance (agree ≥ +0.34, disagree ≤ −0.20).")

    @st.cache_data(ttl=120)
    def _council_frames(db: str, stamp: str):
        return council.logged_votes_frame(engine), council.historical_votes_frame(engine)

    live_df, hist_df = _council_frames(DB, stamp)

    def _show(title: str, df: pd.DataFrame, empty: str):
        sm = council.summarize_votes(df)
        st.markdown(f"**{title}**")
        if not sm["n"]:
            st.info(empty)
            return
        st.caption(f"{sm['n']} labelled signals · overall win rate {sm['base_win']:.0%}"
                   + (" · small sample: treat as noise until n ≳ 100" if sm["n"] < 100 else ""))
        c1, c2 = st.columns(2)
        c1.dataframe(sm["verdicts"], hide_index=True, width="stretch", column_config={
            "win_rate": st.column_config.NumberColumn("win rate", format="percent"),
            "lift_vs_all": st.column_config.NumberColumn("vs all signals", format="%+.1f%%")})
        c2.dataframe(sm["analysts"], hide_index=True, width="stretch", column_config={
            "win_rate": st.column_config.NumberColumn("win rate", format="percent")})

    _show("Paper-run shadow log (the real test)", live_df,
          "No shadow votes with a known outcome yet. Votes are logged when the scheduler sees a signal; outcomes arrive after the post-close labelling.")
    if not live_df.empty:
        st.markdown("**Latest council calls vs what the engine did**")
        show = live_df.sort_values("time", ascending=False).head(25)[["time", "symbol", "direction", "verdict", "score", "action", "label"]]
        st.dataframe(show.rename(columns={"label": "won?"}), hide_index=True, width="stretch")
    _show("History check (feature-based votes on stored, labelled signals; no HTF / sentiment)", hist_df,
          "No labelled signals stored yet. Run the backfill + `python -m src.smc_logic.backfill_signals`.")

# ---- Scanner
with tab_scan:
    st.caption("Confluence = how many of the six conditions agree with the current EMA bias (trend, momentum band, volume spike, "
               "premium/discount zone, swing structure, order block). It is a screening aid, not a prediction — see the ML tab for evidence.")
    sc = pd.DataFrame([{
        "Symbol": r["symbol"], "Price": r.get("price"), "Chg %": r.get("chg_pct"), "Bias": (r.get("bias") or "—").upper(),
        "Last signal": (f"{r['signal'].upper()} · {r['signal_age']}b ago" if r.get("signal") in ("long", "short") else "—"),
        "Confluence": r.get("confluence"), "RSI": r.get("rsi"), "Vol ×avg": r.get("vol_ratio"), "Zone": r.get("zone"),
        "Structure": r.get("structure"), "Sentiment": r.get("sentiment") if r.get("news_n") else None,
    } for r in rows]).sort_values("Confluence", ascending=False, na_position="last")
    st.dataframe(sc, hide_index=True, width="stretch", column_config={
        "Price": st.column_config.NumberColumn(format="%.2f"), "Chg %": st.column_config.NumberColumn(format="%+.2f%%"),
        "Confluence": st.column_config.ProgressColumn(min_value=0, max_value=6, format="%d / 6"),
        "Sentiment": st.column_config.NumberColumn(format="%+.2f"), "RSI": st.column_config.NumberColumn(format="%.1f"),
        "Vol ×avg": st.column_config.NumberColumn(format="%.2f")})
    r = by_sym.get(sym, {})
    if r.get("checks"):
        st.markdown(f"**{sym} — confluence breakdown ({(r.get('bias') or '').upper()} bias)**")
        st.html('<div style="display:flex;gap:8px;flex-wrap:wrap">' + "".join(
            f'<span class="pill {"up" if v else ""}">{"✓" if v else "·"} {T.esc(name)}</span>' for name, v in r["checks"].items()) + "</div>")

# ---- Trades
with tab_trades:
    if trades.empty:
        st.info("No trades yet. Once the scheduler runs (or you load demo data with `python -m src.backtest.replay`) they appear here.")
    else:
        op = trades[trades["status"].isin(["open", "filled"])].copy()
        st.subheader(f"Open positions ({len(op)})")
        if op.empty:
            st.caption("Flat.")
        else:
            op["last"] = op["symbol"].map(lambda s_: by_sym.get(s_, {}).get("price"))
            sign = op["direction"].map({"long": 1, "short": -1})
            op["unrealised"] = (op["last"] - op["entry_price"]) * op["qty"] * sign
            st.dataframe(op[["symbol", "direction", "qty", "entry_price", "last", "unrealised", "stop_loss", "take_profit", "model_probability", "mode"]],
                         hide_index=True, width="stretch")
        st.subheader("Trade log")
        f1, f2 = st.columns(2)
        stat = f1.multiselect("Status", sorted(trades["status"].unique()), default=sorted(trades["status"].unique()))
        syms = f2.multiselect("Symbols", list(tickers), default=[])
        view = trades[trades["status"].isin(stat)]
        view = view[view["symbol"].isin(syms)] if syms else view
        v = view.copy()
        v["entry (CT)"] = m.to_local(v["entry_time"]).dt.strftime("%m-%d %H:%M") if v["entry_time"].notna().any() else None
        v["exit (CT)"] = m.to_local(v["exit_time"]).dt.strftime("%m-%d %H:%M") if v["exit_time"].notna().any() else None
        cols = ["id", "symbol", "direction", "entry (CT)", "exit (CT)", "entry_price", "exit_price", "qty", "pnl", "model_probability",
                "sentiment_at_entry", "stop_loss", "take_profit", "mode", "status", "note"]
        st.dataframe(v.sort_values("id", ascending=False)[cols], hide_index=True, width="stretch", column_config={
            "pnl": st.column_config.NumberColumn("P&L ($)", format="%.2f"), "model_probability": st.column_config.NumberColumn("P(win)", format="%.2f"),
            "sentiment_at_entry": st.column_config.NumberColumn("Sentiment", format="%+.2f")})
        st.download_button("Download CSV", v[cols].to_csv(index=False), "trades.csv", "text/csv")

# ---- Performance
with tab_perf:
    cl = m.closed(trades)
    if cl.empty:
        st.info("No closed trades yet.")
    else:
        a, b = st.columns([3, 2])
        with a:
            st.markdown("**Equity curve**")
            fig = go.Figure()
            tl = m.to_local(eq["time"])
            for mode, color in (("paper", T.ACCENT), ("live", T.AMBER)):
                seg = eq["mode"] == mode
                if seg.any():
                    fig.add_trace(go.Scatter(x=tl[seg], y=eq["equity"][seg], mode="lines+markers", name=mode, line=dict(color=color, width=2),
                                             marker=dict(size=4), fill="tozeroy" if False else None))
            fig.add_hline(y=start_equity, line_dash="dot", line_color=T.MUTED)
            fig.update_layout(**T.PLOT, height=330, yaxis_title="Equity ($)")
            st.plotly_chart(fig, width="stretch")
        with b:
            st.markdown("**P&L per trade**")
            fig = go.Figure(go.Histogram(x=cl["pnl"], nbinsx=25, marker_color=T.ACCENT, opacity=.85))
            fig.add_vline(x=0, line_color=T.MUTED)
            fig.update_layout(**T.PLOT, height=330, xaxis_title="P&L ($)", yaxis_title="Trades")
            st.plotly_chart(fig, width="stretch")
        c, d, e = st.columns(3)
        dp = m.daily_pnl(trades)
        with c:
            st.markdown("**Daily P&L**")
            fig = go.Figure(go.Bar(x=[str(x) for x in dp.index], y=dp.values, marker_color=[T.UP if x >= 0 else T.DOWN for x in dp.values]))
            fig.update_layout(**T.PLOT, height=280)
            st.plotly_chart(fig, width="stretch")
        for col, title, df in ((d, "Win rate by signal type", m.winrate_by_signal(trades)), (e, "Win rate by sentiment at entry", m.winrate_by_sentiment(trades))):
            with col:
                st.markdown(f"**{title}**")
                if df.empty:
                    st.caption("Not enough data yet.")
                    continue
                fig = go.Figure(go.Bar(x=df["group"], y=df["win_rate"] * 100, text=[f"n={int(n)}" for n in df["trades"]], marker_color=T.UP))
                fig.add_hline(y=50, line_dash="dot", line_color=T.MUTED)
                fig.update_layout(**T.PLOT, height=280, yaxis_range=[0, 100], yaxis_title="%")
                st.plotly_chart(fig, width="stretch")
        rs = m.rolling_sharpe(trades, start_equity)
        if len(rs.dropna()) >= 10:
            st.markdown("**Rolling Sharpe (20 trading days)**")
            st.line_chart(rs.dropna())

# ---- Decision log
with tab_dec:
    ev = _events(DB, 24 * 14)
    dec = ev[ev["kind"] == "decision"] if not ev.empty else ev
    if dec.empty:
        st.info("No decisions logged yet.")
    else:
        taken = int(dec["message"].str.contains("-> TRADE", regex=False).sum())
        c1, c2, c3 = st.columns(3)
        c1.metric("Signals evaluated (14d)", len(dec))
        c2.metric("Taken", taken)
        c3.metric("Blocked", len(dec) - taken)
        q = st.text_input("Filter (symbol, gate name, e.g. model, daily_loss_halt, session_cutoff)")
        shown = dec[dec["message"].str.contains(q, case=False, regex=False)] if q else dec
        for r_ in shown.head(60).itertuples():
            ok = "-> TRADE" in r_.message
            with st.expander(f"{'🟢' if ok else '⚪'}  {m.to_local(pd.Series([r_.timestamp])).iat[0]:%m-%d %H:%M}  ·  {r_.message.splitlines()[0]}"):
                st.code(r_.message, language="text")

# ---- ML model
with tab_ml:
    card = D.load_model_card()
    if card is None:
        st.info("No model trained yet. Run `python -m src.ml.train` (needs signals: `python -m src.smc_logic.backfill_signals`).")
    else:
        mt = card.get("metrics", {})
        improves = bool(mt.get("improves"))
        if improves:
            st.html('<div class="banner good"><b>Validated:</b> the filter beat the raw signal out-of-sample. It may gate trades.</div>')
        else:
            st.html('<div class="banner bad"><b>Not validated:</b> out-of-sample, the filter did <b>not</b> beat the raw signal. '
                    'The engine ignores this model unless <code>USE_UNVALIDATED_MODEL=true</code>. That is a legitimate finding about the strategy, not a bug.</div>')
        c = st.columns(5)
        c[0].metric("Model", card.get("version", card["file"]))
        c[1].metric("OOS AUC", f"{mt.get('auc', float('nan')):.3f}" if mt.get("auc") is not None else "—", help="0.5 = no skill")
        unf, flt = mt.get("unfiltered", {}) or {}, mt.get("filtered", {}) or {}
        c[2].metric("Unfiltered win rate", f"{unf.get('win_rate', float('nan')) * 100:.1f}%" if unf else "—", f"n={unf.get('n', '—')}", delta_color="off")
        c[3].metric("Filtered win rate", f"{flt.get('win_rate', float('nan')) * 100:.1f}%" if flt else "—", f"n={flt.get('n', '—')}", delta_color="off")
        c[4].metric("Trained on", f"{card.get('trained_on', {}).get('n', '—')} signals")
        top = card.get("top_features") or {}
        if top:
            fig = go.Figure(go.Bar(x=list(top.values())[::-1], y=list(top.keys())[::-1], orientation="h", marker_color=T.ACCENT))
            fig.update_layout(**T.PLOT, height=320, title="Top features (importance — not evidence of edge)")
            st.plotly_chart(fig, width="stretch")
        with st.expander("MODEL_LOG.md"):
            st.markdown(card.get("log", ""))

# ---- System
with tab_sys:
    latest = max((t for t in (latest_bar_time(engine, x, S.timeframe) for x in tickers) if t), default=None)
    h = m.health(engine, latest, trading_mode=S.trading_mode)
    c = st.columns(5)
    c[0].metric("Status", "OK" if h["status"] == "ok" else "ATTENTION")
    c[1].metric("Mode", h["mode"].upper())
    c[2].metric("Last scheduler cycle", "never" if h["last_cycle"] is None else f"{h['minutes_since_cycle']:.0f} min ago")
    c[3].metric("Errors (24h)", h["errors_24h"])
    c[4].metric("Newest bar", "—" if h["data_stale_minutes"] is None else f"{h['data_stale_minutes']:.0f} min old")
    fresh = []
    for x in tickers:
        t = latest_bar_time(engine, x, S.timeframe)
        fresh.append({"Symbol": x, "Newest bar (CT)": None if t is None else m.to_local(pd.Series([t])).iat[0].strftime("%m-%d %H:%M"),
                      "Age (min)": None if t is None else round((utc_now() - t).total_seconds() / 60)})
    st.dataframe(pd.DataFrame(fresh), hide_index=True, width="stretch")
    ev = _events(DB, 72)
    if not ev.empty:
        kinds = st.multiselect("Event types", sorted(ev["kind"].unique()), default=[x for x in sorted(ev["kind"].unique()) if x not in ("cycle", "decision")])
        e2 = ev[ev["kind"].isin(kinds)].copy()
        e2["time (CT)"] = m.to_local(e2["timestamp"]).dt.strftime("%m-%d %H:%M:%S")
        st.dataframe(e2[["time (CT)", "kind", "message"]].head(300), hide_index=True, width="stretch")

# ---- Settings
with tab_set:
    st.markdown("**Risk limits & mode** (set via `.env`; see `.env.example`)")
    st.dataframe(pd.DataFrame([
        ("Trading mode", S.trading_mode), ("Max position size", f"{S.max_position_pct:.1%} of equity"), ("Risk per trade", f"{S.risk_per_trade_pct:.2%} of equity"),
        ("Daily loss halt", f"{S.max_daily_loss_pct:.1%}"), ("Max open positions", S.max_open_positions), ("Min model probability", S.min_model_probability),
        ("Use unvalidated model", S.use_unvalidated_model), ("Sentiment block |score| ≥", S.sentiment_block_threshold), ("Shorts allowed", S.allow_shorts),
        ("Min reward:risk", S.min_rr), ("Flatten before close", f"{S.flatten_minutes_before_close} min" if S.flatten_at_close else "off"),
        ("No new entries before close", f"{S.no_new_entries_minutes_before_close} min"), ("Extended-hours bars", S.include_extended_hours),
        ("Timeframe", S.timeframe), ("Tickers", ", ".join(S.tickers)),
    ], columns=["Setting", "Value"]).astype(str), hide_index=True, width="stretch")
    st.markdown("**Kill switch** — blocks every new order immediately (open positions are not touched).")
    ks_on = S.kill_switch_active
    st.html(f'<div class="banner {"bad" if ks_on else "good"}">Kill switch is <b>{"ACTIVE" if ks_on else "off"}</b>.</div>')
    ok = st.checkbox("I understand this affects the running scheduler", key="ks_ok")
    if st.button("Deactivate kill switch" if ks_on else "Activate kill switch", disabled=not ok, type="primary" if not ks_on else "secondary"):
        try:
            if ks_on:
                S.kill_switch_file.unlink(missing_ok=True)
            else:
                S.kill_switch_file.write_text(f"activated from dashboard {utc_now().isoformat()}Z\n")
            st.rerun()
        except OSError as exc:
            st.error(f"Could not change the kill switch file: {exc}")
