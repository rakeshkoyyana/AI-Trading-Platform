from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

import src.sentiment.news_fetch as nf
from src.config import settings as settings_mod
from src.db.schema import FetchLog, News, SentimentScore, get_engine, init_db, session_scope
from src.sentiment import finbert_score
from src.sentiment.aggregate import (
    get_rolling_sentiment,
    score_unscored,
    weighted_sentiment,
)

NOW = datetime(2026, 10, 1, 15, 0, 0)


@pytest.fixture()
def engine():
    eng = get_engine("sqlite:///:memory:")
    init_db(eng)
    return eng


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    s = settings_mod.Settings(finnhub_api_key="fh-key", newsapi_key="na-key")
    monkeypatch.setattr(nf, "get_settings", lambda: s)
    finbert_score.set_scorer(None)
    yield
    finbert_score.set_scorer(None)


def _finnhub_payload(n=3):
    base = int((NOW - timedelta(hours=1)).replace(tzinfo=__import__("datetime").timezone.utc).timestamp())
    return [
        dict(datetime=base - i * 600, headline=f"ACME beats estimates #{i}", source="Reuters", url=f"http://x/{i}")
        for i in range(n)
    ]


def test_finnhub_then_cache_prevents_second_call(engine):
    calls = []

    def http(url, params, timeout=15):
        calls.append(url)
        return _finnhub_payload()

    rows = nf.get_news("ACME", NOW - timedelta(hours=6), engine=engine, http=http, now=NOW)
    assert len(rows) == 3 and len(calls) == 1
    # second call inside the TTL: no API traffic, same data
    rows2 = nf.get_news("ACME", NOW - timedelta(hours=6), engine=engine, http=http, now=NOW + timedelta(minutes=5))
    assert len(rows2) == 3 and len(calls) == 1
    # after TTL: refetch, but URL de-dupe means no duplicates stored
    nf.get_news("ACME", NOW - timedelta(hours=6), engine=engine, http=http, now=NOW + timedelta(minutes=45))
    assert len(calls) == 2
    with session_scope(engine) as s:
        assert len(s.execute(select(News)).scalars().all()) == 3


def test_falls_back_to_newsapi_when_finnhub_fails(engine):
    def http(url, params, timeout=15):
        if "finnhub" in url:
            raise RuntimeError("429")
        return {
            "articles": [
                dict(title="ACME wins contract", url="http://n/1", publishedAt="2026-10-01T14:10:00Z",
                     source={"name": "Wire"})
            ]
        }

    rows = nf.get_news("ACME", NOW - timedelta(hours=6), engine=engine, http=http, now=NOW)
    assert [r.headline for r in rows] == ["ACME wins contract"]
    assert rows[0].timestamp == datetime(2026, 10, 1, 14, 10)


def test_missing_keys_do_not_crash(engine, monkeypatch):
    monkeypatch.setattr(nf, "get_settings", lambda: settings_mod.Settings())
    assert nf.get_news("ACME", NOW - timedelta(hours=6), engine=engine, now=NOW) == []
    with session_scope(engine) as s:  # still marks the fetch so we don't hammer on every cycle
        assert s.execute(select(FetchLog)).scalars().first() is not None


def test_lexicon_fallback_scores_sensibly():
    pos = finbert_score.lexicon_scorer(["Company beats earnings, raises guidance"])[0]
    neg = finbert_score.lexicon_scorer(["Shares plunge after lawsuit and downgrade"])[0]
    neu = finbert_score.lexicon_scorer(["Company to hold annual meeting Tuesday"])[0]
    assert pos[0] == "positive" and pos[1] > 0
    assert neg[0] == "negative" and neg[1] < 0
    assert neu == ("neutral", 0.0)


def test_scorer_falls_back_when_finbert_cannot_load(monkeypatch):
    monkeypatch.setattr(finbert_score, "_load_finbert", lambda: (_ for _ in ()).throw(ImportError("no torch")))
    label, score = finbert_score.score_headline("Stock surges on record profit")
    assert finbert_score.scorer_name() == "lexicon-fallback" and label == "positive" and score > 0


def test_score_unscored_is_incremental(engine):
    finbert_score.set_scorer(lambda texts: [("positive", 0.8) for _ in texts], "fake")
    nf.store_articles(
        engine,
        [dict(symbol="ACME", timestamp=NOW - timedelta(minutes=30 * i), headline=f"h{i}", source="s", url=f"u{i}")
         for i in range(4)],
    )
    assert score_unscored(engine) == 4
    assert score_unscored(engine) == 0  # nothing left to score
    with session_scope(engine) as s:
        assert len(s.execute(select(SentimentScore)).scalars().all()) == 4


def test_weighted_sentiment_recency_and_window():
    items = [(NOW - timedelta(minutes=10), 0.9), (NOW - timedelta(hours=3, minutes=50), -0.9),
             (NOW - timedelta(hours=9), -1.0)]  # outside the 4h window
    r = weighted_sentiment(items, NOW, window_hours=4)
    assert r["n"] == 2 and r["score"] > 0.5 and r["label"] == "positive"  # fresh positive dominates
    empty = weighted_sentiment([], NOW)
    assert empty == dict(score=0.0, n=0, label="neutral")
    # all-equal scores -> weighted mean equals the score
    assert weighted_sentiment([(NOW, 0.4), (NOW - timedelta(hours=2), 0.4)], NOW)["score"] == pytest.approx(0.4)


def test_get_rolling_sentiment_reads_db(engine):
    with session_scope(engine) as s:
        s.add(SentimentScore(symbol="ACME", timestamp=NOW - timedelta(minutes=20), score=-0.7, label="negative"))
        s.add(SentimentScore(symbol="OTHER", timestamp=NOW - timedelta(minutes=20), score=0.9, label="positive"))
    r = get_rolling_sentiment("ACME", 4, engine=engine, now=NOW)
    assert r["n"] == 1 and r["score"] == pytest.approx(-0.7) and r["label"] == "negative"
