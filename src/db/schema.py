"""
SQLAlchemy schema for the trading platform.

Tables (per PLAN.md Phase 1): bars, signals, news, sentiment_scores,
trades, model_predictions.

All timestamps are stored as naive UTC datetimes. Convert to America/Chicago
only at the display layer.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from src.config import get_settings


class Base(DeclarativeBase):
    pass


class Bar(Base):
    __tablename__ = "bars"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "timestamp", name="uq_bar"),
        Index("ix_bars_lookup", "symbol", "timeframe", "timestamp"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16))
    timeframe: Mapped[str] = mapped_column(String(8))
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "timestamp", "signal_type", name="uq_signal"),
        Index("ix_signals_symbol_ts", "symbol", "timestamp"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16))
    timeframe: Mapped[str] = mapped_column(String(8), default="15Min")
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    signal_type: Mapped[str] = mapped_column(String(32), default="triple_confirmation")
    direction: Mapped[str] = mapped_column(String(8))  # long | short
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    confirmation_details_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Labels (filled by the labeling step in Phase 2)
    forward_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    label_win: Mapped[int | None] = mapped_column(Integer, nullable=True)


class News(Base):
    __tablename__ = "news"
    __table_args__ = (UniqueConstraint("url", name="uq_news_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    headline: Mapped[str] = mapped_column(String(1000))
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    url: Mapped[str] = mapped_column(String(1000))


class SentimentScore(Base):
    __tablename__ = "sentiment_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    news_id: Mapped[int | None] = mapped_column(ForeignKey("news.id"), nullable=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    score: Mapped[float] = mapped_column(Float)  # -1 (negative) .. +1 (positive)
    label: Mapped[str] = mapped_column(String(16))  # positive | neutral | negative


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    entry_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    direction: Mapped[str] = mapped_column(String(8))
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    qty: Mapped[float] = mapped_column(Float, default=0)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    model_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    sentiment_at_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(8), default="paper")  # paper | live
    # open | filled | closed | rejected | cancelled
    status: Mapped[str] = mapped_column(String(16), default="open")
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)


class TickerMode(Base):
    """Per-ticker trade mode chosen on the dashboard: off | ask | auto."""

    __tablename__ = "ticker_modes"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    mode: Mapped[str] = mapped_column(String(8))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PendingOrder(Base):
    """A trade the engine wants to make on an 'ask' ticker, waiting for the user's Approve / Reject."""

    __tablename__ = "pending_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    qty: Mapped[int] = mapped_column(default=0)
    entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    signal_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reasons_json: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    # pending | approved | rejected | expired | executed | failed
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"), nullable=True)


class CloseRequest(Base):
    """A 'Close position' click on the dashboard; the scheduler (which owns the broker) carries it out."""

    __tablename__ = "close_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    # pending | done | failed | expired
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)


class ModifyRequest(Base):
    """New stop / target for an OPEN position, dragged on the chart; the scheduler (which owns the broker) applies it."""

    __tablename__ = "modify_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    target: Mapped[float | None] = mapped_column(Float, nullable=True)
    # pending | done | failed | expired
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)


class CouncilVote(Base):
    """Shadow analyst-council votes for a fresh signal. Logged beside the real decision; never changes it."""

    __tablename__ = "council_votes"
    __table_args__ = (UniqueConstraint("symbol", "signal_time", "direction", name="uq_council"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), default="15Min")
    signal_time: Mapped[datetime] = mapped_column(DateTime)
    direction: Mapped[str] = mapped_column(String(8))
    votes_json: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    verdict: Mapped[str | None] = mapped_column(String(12), nullable=True)  # agree | mixed | disagree
    action: Mapped[str | None] = mapped_column(String(40), nullable=True)  # traded | pending | blocked:<code> | off
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"), nullable=True)


class ModelPrediction(Base):
    __tablename__ = "model_predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    probability: Mapped[float] = mapped_column(Float)
    model_version: Mapped[str] = mapped_column(String(64))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FetchLog(Base):
    """When each (symbol, source) news fetch last ran — drives API-quota-saving cache TTLs."""

    __tablename__ = "fetch_log"
    __table_args__ = (UniqueConstraint("symbol", "source", name="uq_fetch_log"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[datetime] = mapped_column(DateTime)


class SystemEvent(Base):
    """Heartbeats, errors and session events — feeds the dashboard health panel."""

    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # cycle | error | session | alert | info
    message: Mapped[str] = mapped_column(String(2000), default="")


_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine(url: str | None = None):
    """Return a process-wide engine (or a fresh one when a URL is passed, e.g. in tests)."""
    global _engine, _SessionLocal
    if url is not None:
        return _make_engine(url)
    if _engine is None:
        _engine = _make_engine(get_settings().database_url)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def _make_engine(url: str):
    if url.startswith("sqlite:///"):
        path = Path(url.replace("sqlite:///", "", 1))
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url, future=True)


def init_db(engine=None) -> None:
    Base.metadata.create_all(engine or get_engine())


@contextmanager
def session_scope(engine=None) -> Iterator[Session]:
    eng = engine or get_engine()
    session = sessionmaker(bind=eng, expire_on_commit=False)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
