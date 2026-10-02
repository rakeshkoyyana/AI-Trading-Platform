"""Builds assets/diagrams/architecture.svg + .png (hand-laid-out so the arrows stay readable)."""
from pathlib import Path

HERE = Path(__file__).parent
W, H = 1600, 900
C = dict(data=("#1b2340", "#6c8cff"), eng=("#16302d", "#26a69a"), gate=("#3a2d10", "#f5b041"), out=("#2c1d42", "#b061ff"),
         ctl=("#3a2d10", "#f5b041"))
items, arrows = [], []


def box(x, y, w, h, title, sub="", kind="data", shape="rect"):
    fill, stroke = C[kind]
    r = 18 if shape == "pill" else 10
    items.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>')
    ty = y + h / 2 - (9 if sub else -5)
    items.append(f'<text x="{x + w / 2}" y="{ty}" text-anchor="middle" font-size="16" font-weight="700" fill="#eef0ff">{title}</text>')
    for i, line in enumerate(sub.split("|") if sub else []):
        items.append(f'<text x="{x + w / 2}" y="{ty + 20 + 17 * i}" text-anchor="middle" font-size="12.5" fill="#aab0cc">{line}</text>')
    return dict(l=(x, y + h / 2), r=(x + w, y + h / 2), t=(x + w / 2, y), b=(x + w / 2, y + h))


def arrow(p, q, label="", dash=False, bend=None, color="#8a90ab", lx=None, ly=None):
    (x1, y1), (x2, y2) = p, q
    if bend == "h":  # horizontal-first elbow
        mx = (x1 + x2) / 2
        d = f"M{x1},{y1} C{mx},{y1} {mx},{y2} {x2},{y2}"
    elif bend == "v":
        my = (y1 + y2) / 2
        d = f"M{x1},{y1} C{x1},{my} {x2},{my} {x2},{y2}"
    else:
        d = f"M{x1},{y1} L{x2},{y2}"
    arrows.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.8" {"stroke-dasharray=\'6 5\'" if dash else ""} marker-end="url(#a)"/>')
    if label:
        lx = lx if lx is not None else (x1 + x2) / 2
        ly = ly if ly is not None else (y1 + y2) / 2 - 7
        arrows.append(f'<text x="{lx}" y="{ly}" text-anchor="middle" font-size="12" font-weight="600" fill="{color if color != "#8a90ab" else "#c3c8e6"}" '
                      f'paint-order="stroke" stroke="#0f1220" stroke-width="4">{label}</text>')


def column(x, label):
    items.append(f'<text x="{x}" y="64" font-size="13" font-weight="700" letter-spacing="1.5" fill="#6c8cff">{label}</text>')


column(30, "1 · DATA (FREE TIER)"); column(320, "2 · INGESTION"); column(610, "3 · STORE"); column(890, "4 · SIGNAL"); column(1190, "5 · DECIDE")
items.append('<text x="30" y="545" font-size="13" font-weight="700" letter-spacing="1.5" fill="#b061ff">6 · ORDERS &amp; EXECUTION</text>')

sip = box(30, 90, 240, 84, "Alpaca SIP bars", "consolidated, exact|15 min behind")
iex = box(30, 215, 240, 84, "Alpaca IEX bars", "real-time, free")
news = box(30, 340, 240, 84, "Finnhub / NewsAPI", "headlines")
load = box(320, 90, 250, 84, "Backfill + on-demand loader", "search a ticker, load only that one")
tail = box(320, 215, 250, 84, "Live tail estimator", "IEX price, volume × k|flagged “est.”, never stored", "eng")
fin = box(320, 340, 250, 84, "FinBERT sentiment", "scores each headline", "eng")
db = box(610, 90, 230, 210, "SQLite", "bars · signals · trades|ticker modes|pending approvals|system events")
tc = box(890, 90, 250, 100, "Triple confirmation + SMC", "EMA 9/21 · RSI band · volume spike|order blocks · FVG · BOS", "eng")
ml = box(890, 215, 250, 84, "ML win-probability", "used only if it beats raw signals OOS", "eng")
dec = box(1190, 90, 380, 100, "Decision engine", "risk gates · sizing · protective stop|no fresh signal → nothing happens", "gate")
mode = box(1190, 215, 380, 84, "Trade mode (per ticker, set on dashboard)", "Off = skip   ·   Ask = you approve   ·   Auto = send", "ctl")

ask = box(30, 580, 270, 92, "Ask → pending approval", "you Approve / Reject|expires in 10 min", "gate")
auto = box(330, 580, 230, 92, "Auto → send now", "all risk gates still apply", "out")
pine = box(590, 580, 300, 92, "Pine exit manager", "RSI ≥ 70 / ≤ 30 or EMA trend flip|runs for every mode, even Off", "out")
brk = box(930, 580, 220, 92, "Broker interface", "one API, two backends", "out")
alp = box(1190, 545, 380, 84, "Alpaca paper (primary)", "market entry + protective stop (OTO)|fills and exits are logged to SQLite", "out")
sim = box(1190, 645, 380, 64, "SimBroker", "tests and dry runs", "out")
dash = box(30, 740, 560, 110, "Dashboard (Streamlit)", "TradingView-style chart: blue Long / red Short / purple Close fills|PDH · PDL · PWH · PWL · Trade control panel · P&amp;L", "out")
sch = box(630, 740, 940, 110, "Scheduler  8:30 – 15:00 CT", "bar-close cycle (exits first, then entries by mode) · approvals job every 10 s|"
          "no entries in the last 15 min · flatten 5 min before close · reconcile with broker · Discord alerts", "out")

def path(pts, label="", lx=0, ly=0, dash=False, color="#8a90ab"):
    d = "M" + " L".join(f"{x},{y}" for x, y in pts)
    arrows.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.8" {"stroke-dasharray=\'6 5\'" if dash else ""} marker-end="url(#a)"/>')
    if label:
        arrows.append(f'<text x="{lx}" y="{ly}" text-anchor="middle" font-size="12" font-weight="600" fill="{color if color != "#8a90ab" else "#c3c8e6"}" '
                      f'paint-order="stroke" stroke="#0f1220" stroke-width="4">{label}</text>')


path([(270, 132), (320, 132)]); path([(270, 257), (320, 257)]); path([(270, 382), (320, 382)])
path([(570, 132), (610, 132)])
path([(610, 262), (570, 262)], "k", 590, 252, dash=True)          # calibration reads SIP bars from the store
path([(840, 140), (890, 140)], "", 0, 0)
path([(445, 299), (445, 320), (865, 320), (865, 180), (890, 180)], "live bars (estimate)", 655, 313, color="#26a69a")
path([(1140, 140), (1190, 140)])
path([(1140, 257), (1160, 257), (1160, 120), (1190, 120)])
path([(570, 382), (1176, 382), (1176, 160), (1190, 160)], "sentiment", 900, 375)
path([(1380, 190), (1380, 215)])
path([(1300, 299), (1300, 515), (250, 515), (250, 580)], "Ask", 300, 507, color="#f5b041")
path([(1450, 299), (1450, 500), (445, 500), (445, 580)], "Auto", 520, 492)
path([(300, 640), (318, 640), (318, 700), (1040, 700), (1040, 672)], "approved (re-validated before sending)", 680, 718, color="#f5b041")
path([(445, 672), (445, 686), (1000, 686), (1000, 672)])
path([(890, 626), (930, 626)])
path([(1150, 600), (1170, 600), (1170, 582), (1190, 582)]); path([(1150, 650), (1170, 650), (1170, 672), (1190, 672)])
path([(165, 740), (165, 672)], "Approve / Reject", 260, 726, dash=True, color="#f5b041")

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="Inter, Helvetica, Arial, sans-serif">
<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#8a90ab"/></marker></defs>
<rect width="{W}" height="{H}" fill="#0f1220"/>
<text x="30" y="34" font-size="22" font-weight="800" fill="#eef0ff">AlphaWave — how the pieces connect</text>
{"".join(arrows)}
{"".join(items)}
</svg>'''
(HERE / "architecture.svg").write_text(svg)
print("ok")
