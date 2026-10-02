"""Symbol universe: a searchable master list of US-listed stocks/ETFs plus the user's research watchlist.

How big platforms do it (TradingView, Robinhood): a lightweight *symbol master* (ticker, name, exchange) is
always available for search, while price history is pulled on demand for the symbol you open and cached;
only a short watchlist stays live. We do the same: the master list comes from Alpaca's assets endpoint and is
cached for a day; bars are fetched when a symbol is opened (see data_ingestion.on_demand).

The *trade list* (TICKERS in .env) is separate and is the only thing the scheduler ever trades.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from src.config import PROJECT_ROOT

ASSETS_PATH = PROJECT_ROOT / "data" / "assets.json"
WATCHLIST_PATH = PROJECT_ROOT / "data" / "watchlist.json"
LISTED = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS", "NYSEARCA"}
WATCHLIST_MAX = 12  # each symbol costs one indicator/SMC computation per refresh

# Used only until the real list has been downloaded (or when offline), so search always works.
FALLBACK_ASSETS = [
    ("AAPL", "Apple Inc."), ("MSFT", "Microsoft Corporation"), ("NVDA", "NVIDIA Corporation"), ("AMZN", "Amazon.com, Inc."),
    ("GOOGL", "Alphabet Inc. Class A"), ("GOOG", "Alphabet Inc. Class C"), ("META", "Meta Platforms, Inc."), ("TSLA", "Tesla, Inc."),
    ("AVGO", "Broadcom Inc."), ("AMD", "Advanced Micro Devices, Inc."), ("NFLX", "Netflix, Inc."), ("PLTR", "Palantir Technologies Inc."),
    ("COIN", "Coinbase Global, Inc."), ("HOOD", "Robinhood Markets, Inc."), ("SOFI", "SoFi Technologies, Inc."), ("ASTS", "AST SpaceMobile, Inc."),
    ("JPM", "JPMorgan Chase & Co."), ("BAC", "Bank of America Corporation"), ("V", "Visa Inc."), ("MA", "Mastercard Incorporated"),
    ("WMT", "Walmart Inc."), ("COST", "Costco Wholesale Corporation"), ("DIS", "The Walt Disney Company"), ("BA", "The Boeing Company"),
    ("XOM", "Exxon Mobil Corporation"), ("CVX", "Chevron Corporation"), ("UNH", "UnitedHealth Group Incorporated"), ("LLY", "Eli Lilly and Company"),
    ("ABBV", "AbbVie Inc."), ("PFE", "Pfizer Inc."), ("INTC", "Intel Corporation"), ("MU", "Micron Technology, Inc."),
    ("SMCI", "Super Micro Computer, Inc."), ("ARM", "Arm Holdings plc"), ("CRM", "Salesforce, Inc."), ("ORCL", "Oracle Corporation"),
    ("SPY", "SPDR S&P 500 ETF Trust"), ("QQQ", "Invesco QQQ Trust"), ("IWM", "iShares Russell 2000 ETF"), ("DIA", "SPDR Dow Jones Industrial Average ETF"),
    ("TQQQ", "ProShares UltraPro QQQ"), ("SQQQ", "ProShares UltraPro Short QQQ"), ("XLF", "Financial Select Sector SPDR Fund"), ("XLE", "Energy Select Sector SPDR Fund"),
    ("GLD", "SPDR Gold Shares"), ("TLT", "iShares 20+ Year Treasury Bond ETF"),
]


def _asset_dicts(rows) -> list[dict]:
    return [dict(symbol=s, name=n, exchange="") for s, n in rows]


def fetch_assets_from_alpaca() -> list[dict]:
    """Active, exchange-listed US equities and ETFs (needs ALPACA_API_KEY / ALPACA_SECRET_KEY)."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    from src.config import get_settings

    s = get_settings()
    if not s.alpaca_api_key or not s.alpaca_secret_key:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
    client = TradingClient(s.alpaca_api_key, s.alpaca_secret_key, paper=True)
    out = []
    for a in client.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)):
        ex = str(getattr(a.exchange, "value", a.exchange) or "")
        if ex.upper() in LISTED and a.symbol and "/" not in a.symbol:
            out.append(dict(symbol=a.symbol.upper(), name=a.name or "", exchange=ex))
    return out


def load_assets(path: Path | None = None, max_age_hours: float = 24.0,
                fetcher: Callable[[], list[dict]] = fetch_assets_from_alpaca, now: float | None = None) -> list[dict]:
    """The symbol master. Cached on disk; refreshed when older than `max_age_hours`; never raises."""
    path = path or ASSETS_PATH
    now = time.time() if now is None else now
    cached = None
    try:
        cached = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        pass
    if cached and now - float(cached.get("fetched_at", 0)) < max_age_hours * 3600 and cached.get("assets"):
        return cached["assets"]
    try:
        assets = fetcher()
        if assets:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(dict(fetched_at=now, assets=assets)))
            return assets
    except Exception as exc:  # noqa: BLE001
        print(f"[universe] could not refresh the symbol list: {exc}")
    if cached and cached.get("assets"):
        return cached["assets"]  # stale beats nothing
    return _asset_dicts(FALLBACK_ASSETS)


def search_assets(assets: list[dict], query: str, limit: int = 12) -> list[dict]:
    """Rank: exact ticker, ticker prefix, name prefix, any word starting with the query, name contains."""
    q = (query or "").strip().lower()
    if not q:
        return []
    scored = []
    for a in assets:
        sym, name = a["symbol"].lower(), (a.get("name") or "").lower()
        if sym == q:
            score = 0
        elif sym.startswith(q):
            score = 1
        elif name.startswith(q):
            score = 2
        elif any(w.startswith(q) for w in name.replace(",", " ").replace(".", " ").split()):
            score = 3
        elif q in name:
            score = 4
        else:
            continue
        scored.append((score, len(sym), sym, a))
    scored.sort(key=lambda t: t[:3])
    return [t[3] for t in scored[:limit]]


def is_known_symbol(assets: list[dict], symbol: str) -> bool:
    sym = symbol.upper()
    return any(a["symbol"] == sym for a in assets)


# ------------------------------------------------------------------ research watchlist
def load_watchlist(path: Path | None = None) -> list[str]:
    try:
        raw = json.loads((path or WATCHLIST_PATH).read_text())
        return [str(x).upper() for x in raw][:WATCHLIST_MAX]
    except Exception:  # noqa: BLE001
        return []


def _save(items: list[str], path: Path | None) -> None:
    p = path or WATCHLIST_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items))


def add_to_watchlist(symbol: str, path: Path | None = None) -> tuple[list[str], str | None]:
    """Returns (watchlist, error). Duplicates are ignored; the list is capped at WATCHLIST_MAX."""
    items, sym = load_watchlist(path), symbol.strip().upper()
    if not sym:
        return items, "empty symbol"
    if sym in items:
        return items, None
    if len(items) >= WATCHLIST_MAX:
        return items, f"watchlist is full ({WATCHLIST_MAX}); remove one first"
    items.append(sym)
    _save(items, path)
    return items, None


def remove_from_watchlist(symbol: str, path: Path | None = None) -> list[str]:
    items = [x for x in load_watchlist(path) if x != symbol.strip().upper()]
    _save(items, path)
    return items
