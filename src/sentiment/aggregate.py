"""Score stored news and compute recency-weighted rolling sentiment per symbol."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.db.schema import News, SentimentScore, get_engine, init_db, session_scope
from src.sentiment import finbert_score


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def score_unscored(engine=None, symbol: str | None = None, limit: int = 500) -> int:
    """Run the scorer on news rows that don't have a sentiment score yet."""
    engine = engine or get_engine()
    init_db(engine)
    with session_scope(engine) as s:
        q = (
            select(News)
            .outerjoin(SentimentScore, SentimentScore.news_id == News.id)
            .where(SentimentScore.id.is_(None))
            .order_by(News.timestamp.desc())
            .limit(limit)
        )
        if symbol:
            q = q.where(News.symbol == symbol)
        rows = list(s.execute(q).scalars())
        if not rows:
            return 0
        scored = finbert_score.score_headlines([r.headline for r in rows])
        for r, (label, sc) in zip(rows, scored):
            s.add(
                SentimentScore(
                    news_id=r.id, symbol=r.symbol, timestamp=r.timestamp, score=sc, label=label
                )
            )
    return len(rows)


def weighted_sentiment(
    items: list[tuple[datetime, float]],
    now: datetime,
    window_hours: float = 4.0,
    half_life_hours: float | None = None,
) -> dict:
    """Recency-weighted mean of (timestamp, score) pairs inside the window.

    Weight = 0.5 ** (age / half_life); default half-life = window/3 so the newest third
    of the window dominates. Returns {score, n, label}; score=0, n=0 when there is no news.
    """
    half_life = half_life_hours or window_hours / 3.0
    num = den = 0.0
    n = 0
    for ts, sc in items:
        age_h = (now - ts).total_seconds() / 3600.0
        if age_h < 0 or age_h > window_hours:
            continue
        w = 0.5 ** (age_h / half_life)
        num += w * sc
        den += w
        n += 1
    score = num / den if den > 0 else 0.0
    label = "positive" if score > 0.15 else "negative" if score < -0.15 else "neutral"
    return dict(score=score, n=n, label=label)


def get_rolling_sentiment(
    symbol: str,
    window_hours: float = 4.0,
    engine=None,
    now: datetime | None = None,
    half_life_hours: float | None = None,
) -> dict:
    engine = engine or get_engine()
    now = now or _utcnow()
    since = now - timedelta(hours=window_hours)
    with session_scope(engine) as s:
        rows = s.execute(
            select(SentimentScore.timestamp, SentimentScore.score).where(
                SentimentScore.symbol == symbol, SentimentScore.timestamp >= since
            )
        ).all()
    return weighted_sentiment([(r[0], r[1]) for r in rows], now, window_hours, half_life_hours)


def refresh_sentiment(symbols: list[str], engine=None, window_hours: float = 4.0) -> dict[str, dict]:
    """Every-15-min job: fetch news -> score new headlines -> return rolling sentiment per symbol."""
    from src.sentiment.news_fetch import get_news

    engine = engine or get_engine()
    init_db(engine)
    out = {}
    for sym in symbols:
        try:
            get_news(sym, _utcnow() - timedelta(hours=max(24, window_hours)), engine=engine, min_refresh_minutes=10.0)
            score_unscored(engine, sym)
        except Exception as exc:  # noqa: BLE001
            print(f"[sentiment] refresh failed for {sym}: {exc}")
        out[sym] = get_rolling_sentiment(sym, window_hours, engine=engine)
    return out
