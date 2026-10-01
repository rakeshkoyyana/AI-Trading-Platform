"""
News ingestion: Finnhub primary, NewsAPI fallback, aggressively cached.

Free-tier budgets: Finnhub 60 calls/min; NewsAPI dev tier 100 requests/day (~15 min delayed).
Caching rules:
  * a (symbol, source) is not re-fetched within `min_refresh_minutes` (tracked in fetch_log)
  * articles are de-duplicated by URL, so re-fetching overlapping windows is harmless
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.config import get_settings
from src.db.schema import FetchLog, News, get_engine, init_db, session_scope

FINNHUB_URL = "https://finnhub.io/api/v1/company-news"
NEWSAPI_URL = "https://newsapi.org/v2/everything"

HttpGet = Callable[..., dict | list]


def _default_get(url: str, params: dict, timeout: float = 15.0):
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fetch_finnhub(symbol: str, since: datetime, until: datetime, http: HttpGet = _default_get) -> list[dict]:
    key = get_settings().finnhub_api_key
    if not key:
        raise RuntimeError("FINNHUB_API_KEY not set")
    data = http(
        FINNHUB_URL,
        {"symbol": symbol, "from": since.strftime("%Y-%m-%d"), "to": until.strftime("%Y-%m-%d"), "token": key},
    )
    out = []
    for a in data or []:
        if not a.get("headline") or not a.get("url"):
            continue
        out.append(
            dict(
                symbol=symbol,
                timestamp=datetime.fromtimestamp(int(a.get("datetime", 0)), tz=timezone.utc).replace(tzinfo=None),
                headline=a["headline"][:1000],
                source=(a.get("source") or "finnhub")[:128],
                url=a["url"][:1000],
            )
        )
    return out


def fetch_newsapi(symbol: str, since: datetime, until: datetime, http: HttpGet = _default_get) -> list[dict]:
    key = get_settings().newsapi_key
    if not key:
        raise RuntimeError("NEWSAPI_KEY not set")
    data = http(
        NEWSAPI_URL,
        {
            "q": symbol,
            "from": since.strftime("%Y-%m-%dT%H:%M:%S"),
            "to": until.strftime("%Y-%m-%dT%H:%M:%S"),
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 50,
            "apiKey": key,
        },
    )
    out = []
    for a in (data or {}).get("articles", []):
        if not a.get("title") or not a.get("url"):
            continue
        ts = datetime.fromisoformat(a["publishedAt"].replace("Z", "+00:00")).astimezone(timezone.utc)
        out.append(
            dict(
                symbol=symbol,
                timestamp=ts.replace(tzinfo=None),
                headline=a["title"][:1000],
                source=((a.get("source") or {}).get("name") or "newsapi")[:128],
                url=a["url"][:1000],
            )
        )
    return out


def _recently_fetched(engine, symbol: str, source: str, within_minutes: float, now: datetime) -> bool:
    with session_scope(engine) as s:
        row = s.execute(
            select(FetchLog).where(FetchLog.symbol == symbol, FetchLog.source == source)
        ).scalar_one_or_none()
    return bool(row and (now - row.fetched_at) < timedelta(minutes=within_minutes))


def _mark_fetched(engine, symbol: str, source: str, now: datetime) -> None:
    with session_scope(engine) as s:
        row = s.execute(
            select(FetchLog).where(FetchLog.symbol == symbol, FetchLog.source == source)
        ).scalar_one_or_none()
        if row is None:
            s.add(FetchLog(symbol=symbol, source=source, fetched_at=now))
        else:
            row.fetched_at = now


def store_articles(engine, articles: list[dict]) -> int:
    """Insert new articles (unique by URL). Returns how many were new."""
    if not articles:
        return 0
    with session_scope(engine) as s:
        existing = set(
            s.execute(select(News.url).where(News.url.in_([a["url"] for a in articles]))).scalars()
        )
        fresh = [a for a in articles if a["url"] not in existing]
        seen = set()
        uniq = []
        for a in fresh:
            if a["url"] not in seen:
                seen.add(a["url"])
                uniq.append(a)
        for a in uniq:
            s.add(News(**a))
    return len(uniq)


def get_news(
    symbol: str,
    since: datetime,
    engine=None,
    min_refresh_minutes: float = 30.0,
    http: HttpGet = _default_get,
    now: datetime | None = None,
) -> list[News]:
    """Return stored news for `symbol` since `since`, refreshing from the APIs only when stale.

    Finnhub is tried first; NewsAPI is used if Finnhub raises or returns nothing.
    """
    engine = engine or get_engine()
    init_db(engine)
    now = now or _utcnow()

    if not _recently_fetched(engine, symbol, "any", min_refresh_minutes, now):
        got: list[dict] = []
        for name, fn in (("finnhub", fetch_finnhub), ("newsapi", fetch_newsapi)):
            try:
                got = fn(symbol, since, now, http=http)
            except Exception as exc:  # noqa: BLE001
                print(f"[news] {name} failed for {symbol}: {exc}")
                got = []
            if got:
                break
        added = store_articles(engine, got)
        _mark_fetched(engine, symbol, "any", now)
        print(f"[news] {symbol}: {len(got)} fetched, {added} new")

    with session_scope(engine) as s:
        return list(
            s.execute(
                select(News)
                .where(News.symbol == symbol, News.timestamp >= since)
                .order_by(News.timestamp.desc())
            ).scalars()
        )


def refresh_all(symbols: list[str], hours: int = 24, engine=None, sleep: float = 1.1) -> dict[str, int]:
    """Refresh news for each symbol, spacing calls to stay inside the 60/min Finnhub limit."""
    since = _utcnow() - timedelta(hours=hours)
    out = {}
    for sym in symbols:
        out[sym] = len(get_news(sym, since, engine=engine))
        time.sleep(sleep)
    return out
