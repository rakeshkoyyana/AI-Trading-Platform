"""
News ingestion from several free sources, merged, filtered for relevance and de-duplicated.

Sources (all free):
  * Alpaca News (Benzinga wire; uses your existing Alpaca keys; near real-time, tagged with tickers)
  * Finnhub company news (60 calls/min)
  * NewsAPI dev tier (100 requests/day, ~15 min delayed): last resort, used only when the others ERROR
Reliability rules:
  * every configured source is asked each refresh and the results are merged (one source down != no news)
  * an article only counts for a ticker if it is really about it (ticker in the text, or the only ticker tagged);
    this drops market-wrap / other-company stories that aggregators tag with many symbols
  * duplicates are dropped by URL and by normalized headline (the same story syndicated by several outlets)
  * each source's health is recorded (data/run/news_health.json) and shown on the dashboard; 3 failures in a row
    raise a Discord warning
  * a (symbol, source) is not re-fetched within `min_refresh_minutes` (tracked in fetch_log)
Diagnose live:  python -m src.sentiment.news_fetch --check ASTS
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.config import PROJECT_ROOT, get_settings
from src.db.schema import FetchLog, News, get_engine, init_db, session_scope

FINNHUB_URL = "https://finnhub.io/api/v1/company-news"
ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
HEALTH_PATH = PROJECT_ROOT / "data" / "run" / "news_health.json"
ALERT_AFTER_FAILURES = 3
NEWSAPI_URL = "https://newsapi.org/v2/everything"

HttpGet = Callable[..., dict | list]


class NotConfigured(RuntimeError):
    """A source has no API key: skipped quietly (not a failure)."""


def _default_get(url: str, params: dict, timeout: float = 15.0, headers: dict | None = None):
    r = requests.get(url, params=params, timeout=timeout, headers=headers)
    r.raise_for_status()
    return r.json()


def _safe_err(exc: Exception) -> str:
    """Error text without API keys (they travel in URLs / params)."""
    msg = str(exc)
    s = get_settings()
    for k in (s.finnhub_api_key, s.newsapi_key, s.alpaca_api_key, s.alpaca_secret_key):
        if k:
            msg = msg.replace(k, "<key>")
    return re.sub(r"(token|apiKey)=[^&\s)']+", r"\1=<key>", msg)[:300]


def is_relevant(symbol: str, headline: str, summary: str = "", tagged: list[str] | None = None) -> bool:
    """Is this article really about `symbol`? Aggregators tag market wraps and peer stories with many tickers."""
    sym = symbol.upper()
    text = f"{headline} {summary}"
    if re.search(rf"(?<![A-Za-z0-9]){re.escape(sym)}(?![A-Za-z0-9])", text):
        return True
    tags = [x.strip().upper() for x in (tagged or []) if x and x.strip()]
    return tags == [sym]  # the only ticker the provider attached


def norm_headline(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", h.lower())[:140]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fetch_finnhub(symbol: str, since: datetime, until: datetime, http: HttpGet = _default_get) -> list[dict]:
    key = get_settings().finnhub_api_key
    if not key:
        raise NotConfigured("FINNHUB_API_KEY not set")
    data = http(
        FINNHUB_URL,
        {"symbol": symbol, "from": since.strftime("%Y-%m-%d"), "to": until.strftime("%Y-%m-%d"), "token": key},
    )
    out = []
    for a in data or []:
        if not a.get("headline") or not a.get("url"):
            continue
        if not is_relevant(symbol, a["headline"], a.get("summary") or "", str(a.get("related") or "").split(",")):
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


def fetch_alpaca(symbol: str, since: datetime, until: datetime, http: HttpGet = _default_get) -> list[dict]:
    s = get_settings()
    if not (s.alpaca_api_key and s.alpaca_secret_key):
        raise NotConfigured("ALPACA keys not set")
    data = http(
        ALPACA_NEWS_URL,
        {"symbols": symbol, "start": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "limit": 50, "sort": "desc", "include_content": "false"},
        headers={"APCA-API-KEY-ID": s.alpaca_api_key, "APCA-API-SECRET-KEY": s.alpaca_secret_key},
    )
    out = []
    for a in (data or {}).get("news", []):
        head = a.get("headline")
        if not head or not is_relevant(symbol, head, a.get("summary") or "", a.get("symbols")):
            continue
        ts = datetime.fromisoformat(str(a["created_at"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        out.append(
            dict(
                symbol=symbol,
                timestamp=ts.replace(tzinfo=None),
                headline=head[:1000],
                source=(a.get("source") or "alpaca")[:128],
                url=(a.get("url") or f"alpaca-news:{a.get('id')}")[:1000],
            )
        )
    return out


def fetch_newsapi(symbol: str, since: datetime, until: datetime, http: HttpGet = _default_get) -> list[dict]:
    key = get_settings().newsapi_key
    if not key:
        raise NotConfigured("NEWSAPI_KEY not set")
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
        if not is_relevant(symbol, a["title"], a.get("description") or ""):
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
        oldest = min(a["timestamp"] for a in articles) - timedelta(days=2)
        heads = {
            (sym, norm_headline(h))
            for sym, h in s.execute(
                select(News.symbol, News.headline).where(
                    News.symbol.in_({a["symbol"] for a in articles}), News.timestamp >= oldest
                )
            )
        }  # the same story syndicated under another URL
        seen = set()
        uniq = []
        for a in fresh:
            key = (a["symbol"], norm_headline(a["headline"]))
            if a["url"] not in seen and key not in heads:
                seen.add(a["url"])
                heads.add(key)
                uniq.append(a)
        for a in uniq:
            s.add(News(**a))
    return len(uniq)


def read_health(path: Path | None = None) -> dict:
    try:
        return json.loads((path or HEALTH_PATH).read_text())
    except Exception:  # noqa: BLE001
        return {}


def record_health(provider: str, ok: bool, count: int = 0, error: str = "", now: datetime | None = None,
                  path: Path | None = None, notify: Callable[[str, str], object] | None = None) -> None:
    """Remember how each source is doing; warn on Discord after ALERT_AFTER_FAILURES failures in a row. Never raises."""
    try:
        p = path or HEALTH_PATH
        h = read_health(p)
        e = h.get(provider, {})
        now_s = (now or _utcnow()).strftime("%Y-%m-%d %H:%M:%S")
        if ok:
            if e.get("alerted"):
                (notify or _notify)(f"News source '{provider}' is working again.", "info")
            e = dict(e, last_ok=now_s, last_count=count, fails=0, alerted=False, error="")
            if count:
                e["last_articles"] = now_s
        else:
            fails = int(e.get("fails", 0)) + 1
            e = dict(e, fails=fails, error=error, last_error=now_s)
            if fails >= ALERT_AFTER_FAILURES and not e.get("alerted"):
                (notify or _notify)(f"News source '{provider}' has failed {fails} times in a row: {error}", "warning")
                e["alerted"] = True
        h[provider] = e
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(h))
    except Exception:  # noqa: BLE001
        pass


def _notify(msg: str, level: str) -> None:
    from src import alerts

    alerts.notify(msg, level)


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
        errored = False
        configured = 0
        for name, fn in (("alpaca", fetch_alpaca), ("finnhub", fetch_finnhub)):
            try:
                items = fn(symbol, since, now, http=http)
                configured += 1
                record_health(name, True, len(items), now=now)
            except NotConfigured:
                continue
            except Exception as exc:  # noqa: BLE001
                configured += 1
                errored = True
                items = []
                print(f"[news] {name} failed for {symbol}: {_safe_err(exc)}")
                record_health(name, False, error=_safe_err(exc), now=now)
            got += items
        if not got and (errored or configured == 0):  # last resort, only when the main sources broke or are absent
            try:
                got = fetch_newsapi(symbol, since, now, http=http)
                record_health("newsapi", True, len(got), now=now)
            except NotConfigured:
                pass
            except Exception as exc:  # noqa: BLE001
                print(f"[news] newsapi failed for {symbol}: {_safe_err(exc)}")
                record_health("newsapi", False, error=_safe_err(exc), now=now)
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


def check(symbol: str, hours: int = 48) -> None:
    """Ask every source live and show what each returns (run this on the machine that has the keys)."""
    now = _utcnow()
    since = now - timedelta(hours=hours)
    print(f"News check for {symbol}, last {hours} h (UTC now {now:%Y-%m-%d %H:%M})")
    for name, fn in (("alpaca", fetch_alpaca), ("finnhub", fetch_finnhub), ("newsapi", fetch_newsapi)):
        try:
            items = fn(symbol, since, now)
        except NotConfigured as exc:
            print(f"  {name:8s} not configured ({exc})")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"  {name:8s} FAILED: {_safe_err(exc)}")
            continue
        if not items:
            print(f"  {name:8s} ok, but 0 relevant articles")
            continue
        newest = max(i["timestamp"] for i in items)
        print(f"  {name:8s} ok, {len(items)} relevant, newest {newest:%m-%d %H:%M} UTC ({int((now - newest).total_seconds() // 60)} min ago)")
        for i in sorted(items, key=lambda x: x["timestamp"], reverse=True)[:3]:
            print(f"             {i['timestamp']:%m-%d %H:%M}  [{i['source']}] {i['headline'][:90]}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--check":
        check(sys.argv[2].upper())
    else:
        print("usage: python -m src.sentiment.news_fetch --check TICKER")
