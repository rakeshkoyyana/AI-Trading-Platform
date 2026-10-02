"""Visual layer: CSS and small HTML builders (no Streamlit imports; everything returns strings)."""
from __future__ import annotations

import html
from datetime import datetime, timezone

from src.dashboard.brand import lockup_img

BG, PANEL, PANEL2, BORDER = "#0e1019", "#1b1e2e", "#222640", "#2a2e45"
TEXT, MUTED, UP, DOWN, ACCENT, AMBER = "#e6e8f2", "#8a90ab", "#26a69a", "#ef5350", "#6c8cff", "#f5b041"

CSS = f"""
<style>
:root {{ --bg:{BG}; --panel:{PANEL}; --panel2:{PANEL2}; --border:{BORDER}; --text:{TEXT}; --muted:{MUTED};
  --up:{UP}; --down:{DOWN}; --accent:{ACCENT}; --amber:{AMBER}; }}
[data-testid="stHeader"], #MainMenu, footer {{ display:none !important; }}
.stApp {{ background: radial-gradient(1200px 500px at 20% -10%, #1a1f3a 0%, {BG} 55%) fixed; }}
.block-container {{ padding: 0.6rem 1.2rem 2rem 1.2rem !important; max-width: 100% !important; }}
[data-testid="stSidebar"] {{ background:{PANEL}; border-right:1px solid {BORDER}; }}
h1,h2,h3,h4 {{ letter-spacing:.2px; }}
/* top bar */
.tp-top {{ display:flex; align-items:center; gap:14px; padding:10px 14px; background:{PANEL}; border:1px solid {BORDER};
  border-radius:14px; margin-bottom:8px; flex-wrap:wrap; }}
.tp-logo {{ font-weight:800; font-size:17px; letter-spacing:.4px; }}
.tp-logo span {{ color:{ACCENT}; }}
.pill {{ display:inline-flex; align-items:center; gap:6px; padding:3px 10px; border-radius:999px; font-size:11.5px; font-weight:600;
  border:1px solid {BORDER}; background:{BG}; color:{MUTED}; white-space:nowrap; }}
.pill.up {{ color:{UP}; border-color:rgba(38,166,154,.45); background:rgba(38,166,154,.10); }}
.pill.down {{ color:{DOWN}; border-color:rgba(239,83,80,.45); background:rgba(239,83,80,.10); }}
.pill.amber {{ color:{AMBER}; border-color:rgba(245,176,65,.45); background:rgba(245,176,65,.10); }}
.pill.accent {{ color:{ACCENT}; border-color:rgba(108,140,255,.45); background:rgba(108,140,255,.10); }}
.dot {{ width:7px; height:7px; border-radius:50%; background:currentColor; display:inline-block; }}
.dot.live {{ animation: pulse 1.6s infinite; }}
@keyframes pulse {{ 0%{{opacity:1}} 50%{{opacity:.25}} 100%{{opacity:1}} }}
.tp-spacer {{ flex:1; }}
.tp-meta {{ color:{MUTED}; font-size:12px; }}
/* ticker tape */
.tape {{ overflow:hidden; background:{PANEL}; border:1px solid {BORDER}; border-radius:12px; margin-bottom:10px; position:relative; }}
.tape::before, .tape::after {{ content:""; position:absolute; top:0; bottom:0; width:42px; z-index:2; pointer-events:none; }}
.tape::before {{ left:0; background:linear-gradient(90deg,{PANEL},transparent); }}
.tape::after {{ right:0; background:linear-gradient(270deg,{PANEL},transparent); }}
.tape-track {{ display:flex; width:max-content; animation: tape var(--dur,70s) linear infinite; padding:9px 0; }}
.tape:hover .tape-track {{ animation-play-state: paused; }}
@keyframes tape {{ from {{ transform: translateX(0); }} to {{ transform: translateX(-50%); }} }}
.ti {{ display:inline-flex; align-items:center; gap:7px; padding:0 22px; border-right:1px solid {BORDER}; font-size:12.5px; white-space:nowrap; }}
.ti b {{ font-size:12.5px; }}
.ti .sym {{ color:{TEXT}; font-weight:700; }}
.ti .nw {{ color:#c8cce0; max-width:520px; overflow:hidden; text-overflow:ellipsis; }}
.up {{ color:{UP}; }} .down {{ color:{DOWN}; }} .mut {{ color:{MUTED}; }}
/* KPI cards */
.kpis {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap:10px; margin-bottom:10px; }}
.kpi {{ background:{PANEL}; border:1px solid {BORDER}; border-radius:14px; padding:11px 13px 9px 13px; position:relative; overflow:hidden; }}
.kpi .l {{ color:{MUTED}; font-size:11px; text-transform:uppercase; letter-spacing:.7px; }}
.kpi .v {{ font-size:22px; font-weight:700; margin-top:2px; font-variant-numeric: tabular-nums; }}
.kpi .s {{ color:{MUTED}; font-size:11px; margin-top:1px; }}
.kpi svg {{ position:absolute; right:8px; bottom:8px; opacity:.9; }}
/* panels */
.panel {{ background:{PANEL}; border:1px solid {BORDER}; border-radius:14px; padding:10px 12px; margin-bottom:10px; }}
.panel h4 {{ margin:0 0 8px 0; font-size:12px; text-transform:uppercase; letter-spacing:.8px; color:{MUTED}; font-weight:600;
  display:flex; justify-content:space-between; align-items:center; }}
.scroll {{ overflow-y:auto; padding-right:4px; }}
.scroll::-webkit-scrollbar {{ width:6px; }} .scroll::-webkit-scrollbar-thumb {{ background:{BORDER}; border-radius:6px; }}
/* watchlist */
.wl {{ display:grid; grid-template-columns: 1.1fr 1fr 70px; gap:8px; align-items:center; padding:8px 4px; border-bottom:1px solid rgba(42,46,69,.7); }}
.wl:last-child {{ border-bottom:0; }}
.wl.sel {{ background:rgba(108,140,255,.09); border-radius:8px; }}
.wl .s {{ font-weight:700; }} .wl .p {{ text-align:right; font-variant-numeric:tabular-nums; }}
.wl .sub {{ color:{MUTED}; font-size:11px; display:flex; gap:5px; align-items:center; flex-wrap:wrap; margin-top:2px; }}
.dots i {{ display:inline-block; width:6px; height:6px; border-radius:50%; background:{BORDER}; margin-right:2px; }}
.dots i.on {{ background:{ACCENT}; }}
/* news */
.nw-item {{ padding:8px 2px; border-bottom:1px solid rgba(42,46,69,.7); }}
.nw-item:last-child {{ border-bottom:0; }}
.nw-item a {{ color:{TEXT}; text-decoration:none; font-size:12.5px; line-height:1.35; }}
.nw-item a:hover {{ color:{ACCENT}; }}
.nw-meta {{ display:flex; gap:6px; align-items:center; color:{MUTED}; font-size:11px; margin-top:3px; flex-wrap:wrap; }}
.chip {{ padding:1px 7px; border-radius:6px; font-size:10.5px; font-weight:700; }}
.chip.pos {{ background:rgba(38,166,154,.16); color:{UP}; }} .chip.neg {{ background:rgba(239,83,80,.16); color:{DOWN}; }}
.chip.neu {{ background:rgba(138,144,171,.16); color:{MUTED}; }} .chip.sym {{ background:rgba(108,140,255,.16); color:{ACCENT}; }}
.empty {{ color:{MUTED}; font-size:12.5px; padding:10px 2px; }}
/* decisions */
.dc {{ padding:7px 2px; border-bottom:1px solid rgba(42,46,69,.7); font-size:12px; color:#c8cce0; }}
.dc:last-child {{ border-bottom:0; }}
.dc .t {{ color:{MUTED}; font-size:10.5px; }}
/* tabs & widgets */
button[data-baseweb="tab"] {{ font-weight:600; }}
[data-testid="stMetric"] {{ background:{PANEL}; border:1px solid {BORDER}; border-radius:12px; padding:10px 12px; }}
[data-testid="stDataFrame"] {{ border:1px solid {BORDER}; border-radius:12px; overflow:hidden; }}
iframe {{ border-radius:12px; }}
.banner {{ border-radius:12px; padding:10px 14px; border:1px solid {BORDER}; margin-bottom:10px; font-size:13px; }}
.banner.warn {{ border-color:rgba(245,176,65,.5); background:rgba(245,176,65,.08); }}
.banner.alert {{ border-color:rgba(245,176,65,.9); background:rgba(245,176,65,.16); font-size:15px; padding:12px 16px; animation:aw-pulse 1.6s ease-in-out infinite; }}
@keyframes aw-pulse {{ 0%,100% {{ box-shadow:0 0 0 0 rgba(245,176,65,.45); }} 50% {{ box-shadow:0 0 0 8px rgba(245,176,65,0); }} }}
.banner.bad {{ border-color:rgba(239,83,80,.5); background:rgba(239,83,80,.08); }}
.banner.good {{ border-color:rgba(38,166,154,.5); background:rgba(38,166,154,.08); }}
</style>
"""

PLOT = dict(
    template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color=MUTED, size=11), margin=dict(l=10, r=10, t=30, b=10),
    xaxis=dict(gridcolor="rgba(42,46,69,.6)", zeroline=False), yaxis=dict(gridcolor="rgba(42,46,69,.6)", zeroline=False),
    legend=dict(orientation="h", y=1.1, x=0),
)


def esc(s) -> str:
    return html.escape("" if s is None else str(s))


def money(x, signed=False) -> str:
    if x is None:
        return "—"
    s = f"${abs(x):,.2f}"
    return ("-" if x < 0 else "+" if signed and x > 0 else "") + s


def pct(x, nd=1, signed=False) -> str:
    if x is None:
        return "—"
    return f"{'+' if signed and x > 0 else ''}{x:.{nd}f}%"


def tone(x) -> str:
    return "mut" if x is None or x == 0 else "up" if x > 0 else "down"


def ago(ts, now=None) -> str:
    if ts is None:
        return "—"
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    s = max((now - ts).total_seconds(), 0)
    return f"{int(s)}s ago" if s < 90 else f"{int(s // 60)}m ago" if s < 5400 else f"{int(s // 3600)}h ago" if s < 172800 else f"{int(s // 86400)}d ago"


def spark_svg(values, color=ACCENT, w=92, h=28, fill=True) -> str:
    v = [float(x) for x in values if x is not None]
    if len(v) < 2:
        return ""
    lo, hi = min(v), max(v)
    rng = (hi - lo) or 1.0
    pts = [(i * (w - 2) / (len(v) - 1) + 1, h - 2 - (x - lo) / rng * (h - 4)) for i, x in enumerate(v)]
    d = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f'<polygon points="1,{h} {d} {w - 1},{h}" fill="{color}" opacity=".13"/>' if fill else ""
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">{area}'
            f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>')


def sent_chip(score) -> str:
    if score is None:
        return '<span class="chip neu">no news</span>'
    cls, lab = ("pos", "Bullish") if score > 0.2 else ("neg", "Bearish") if score < -0.2 else ("neu", "Neutral")
    return f'<span class="chip {cls}">{lab} {score:+.2f}</span>'


def pill(text, kind="", live=False) -> str:
    dot = '<span class="dot live"></span>' if live else ""
    return f'<span class="pill {kind}">{dot}{esc(text)}</span>'


def topbar(mode_live: bool, market_open: bool, next_open: str | None, kill: bool, equity: float, as_of: str) -> str:
    return (
        '<div class="tp-top"><div class="tp-logo">' + lockup_img(28) + '</div>'
        + pill("LIVE TRADING" if mode_live else "PAPER", "down" if mode_live else "up", live=True)
        + pill("Market open" if market_open else f"Market closed{' · opens ' + next_open if next_open else ''}", "up" if market_open else "amber", live=market_open)
        + (pill("KILL SWITCH ON", "down") if kill else "")
        + '<span class="tp-spacer"></span>'
        + f'<span class="tp-meta">Realised equity <b style="color:{TEXT}">{money(equity)}</b></span>'
        + f'<span class="tp-meta">Updated {esc(as_of)}</span></div>'
    )


def ticker_tape(rows: list[dict], news: list[dict]) -> str:
    items = []
    for r in rows:
        if r.get("price") is None:
            continue
        c = r.get("chg_pct")
        arrow = "▲" if (c or 0) >= 0 else "▼"
        items.append(f'<span class="ti"><span class="sym">{esc(r["symbol"])}</span><b>{r["price"]:,.2f}</b>'
                     f'<span class="{tone(c)}">{arrow} {pct(abs(c) if c is not None else None, 2)}</span></span>')
    for n in news[:14]:
        sc = n.get("score")
        cls = "mut" if sc is None else "up" if sc > 0.2 else "down" if sc < -0.2 else "mut"
        items.append(f'<span class="ti"><span class="chip sym">{esc(n["symbol"])}</span><span class="nw">{esc(n["headline"])}</span>'
                     f'<span class="{cls}">{"" if sc is None else f"{sc:+.2f}"}</span></span>')
    if not items:
        return '<div class="tape"><div class="tape-track"><span class="ti mut">Waiting for market data…</span></div></div>'
    body = "".join(items)
    dur = max(40, len(items) * 6)
    return f'<div class="tape"><div class="tape-track" style="--dur:{dur}s">{body}{body}</div></div>'


def kpi(label: str, value: str, sub: str = "", spark=None, color=ACCENT, cls: str = "") -> str:
    sp = spark_svg(spark, color, 74, 26) if spark else ""
    return f'<div class="kpi"><div class="l">{esc(label)}</div><div class="v {cls}">{value}</div><div class="s">{sub}</div>{sp}</div>'


def watchlist(rows: list[dict], selected: str) -> str:
    out = []
    for r in rows:
        if r.get("price") is None:
            out.append(f'<div class="wl"><div class="s">{esc(r["symbol"])}</div><div class="mut">no data</div><div></div></div>')
            continue
        c = r.get("chg_pct")
        col = UP if (c or 0) >= 0 else DOWN
        conf, cmax = r.get("confluence"), r.get("confluence_max") or 6
        dots = "".join(f'<i class="{"on" if i < (conf or 0) else ""}"></i>' for i in range(cmax)) if conf is not None else ""
        sig = r.get("signal")
        sig_chip = (f'<span class="chip {"pos" if sig == "long" else "neg"}">{"LONG" if sig == "long" else "SHORT"} · {r.get("signal_age")}b</span>'
                    if sig in ("long", "short") and (r.get("signal_age") is not None and r["signal_age"] <= 3) else "")
        out.append(
            f'<div class="wl {"sel" if r["symbol"] == selected else ""}">'
            f'<div><div class="s">{esc(r["symbol"])}</div><div class="sub">{sig_chip}<span class="dots" title="Confluence {conf}/{cmax}">{dots}</span></div></div>'
            f'<div class="p"><div>{r["price"]:,.2f}</div><div class="{tone(c)}" style="font-size:11.5px">{pct(c, 2, True)}</div>'
            f'<div style="margin-top:2px">{sent_chip(r.get("sentiment") if r.get("news_n") else None)}</div></div>'
            f'<div>{spark_svg(r.get("spark"), col, 70, 30)}</div></div>'
        )
    return "".join(out) or '<div class="empty">No symbols.</div>'


def news_list(items: list[dict], now=None) -> str:
    if not items:
        return '<div class="empty">No news stored yet. The scheduler refreshes it hourly (needs Finnhub/NewsAPI keys), or press “Fetch latest”.</div>'
    out = []
    for n in items:
        sc = n.get("score")
        chip = "" if sc is None else sent_chip(sc)
        u = str(n.get("url") or "")
        link = esc(u if u.lower().startswith(("http://", "https://")) else "#")  # never emit javascript:/data: links
        out.append(f'<div class="nw-item"><a href="{link}" target="_blank" rel="noopener">{esc(n["headline"])}</a>'
                   f'<div class="nw-meta"><span class="chip sym">{esc(n["symbol"])}</span>{chip}<span>{esc(n.get("source") or "")}</span><span>· {ago(n["timestamp"], now)}</span></div></div>')
    return "".join(out)


def decision_list(items: list[dict], now=None) -> str:
    if not items:
        return '<div class="empty">No decisions logged yet — every signal (taken or blocked) lands here with the reason.</div>'
    return "".join(f'<div class="dc"><div class="t">{ago(i["timestamp"], now)}</div>{esc(i["message"])}</div>' for i in items)


def recent_trades(items: list[dict], now=None) -> str:
    """Compact trade tape: newest first. Each item: symbol, direction, qty, entry_price, exit_price, pnl, status, time."""
    if not items:
        return '<div class="empty">No trades yet.</div>'
    out = []
    for r in items:
        side = f'<span class="chip {"pos" if r["direction"] == "long" else "neg"}">{"LONG" if r["direction"] == "long" else "SHORT"}</span>'
        entry = f'{r["entry_price"]:.2f}' if r.get("entry_price") is not None else "—"
        if r.get("status") == "closed" and r.get("pnl") is not None:
            ex = f'{r["exit_price"]:.2f}' if r.get("exit_price") is not None else "—"
            res = f'<b class="{tone(r["pnl"])}">{money(r["pnl"], True)}</b>'
            mid = f"{entry} → {ex}"
        else:
            res = f'<span class="chip sym">{esc(r.get("status", "open"))}</span>'
            mid = f"entry {entry}"
        out.append(f'<div class="wl" style="grid-template-columns: 70px 1fr 1fr 90px 80px;"><div class="s">{esc(r["symbol"])}</div><div>{side} <span class="mut">×{r["qty"]:g}</span></div>'
                   f'<div class="mut">{mid}</div><div style="text-align:right">{res}</div><div class="mut" style="text-align:right;font-size:11px">{ago(r.get("time"), now)}</div></div>')
    return "".join(out)
