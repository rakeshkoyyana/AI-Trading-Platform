"""Per-ticker trade control (Off / Ask / Auto) and the approval queue.

* off  - the platform never opens a position in this ticker (an existing one is still managed/closed).
* ask  - a qualifying signal becomes a PendingOrder; nothing is sent to the broker until you Approve.
* auto - a qualifying signal is sent to the broker immediately (all risk gates still apply).

Modes are stored in `ticker_modes`; a ticker with no stored mode uses DEFAULT_TRADE_MODE if it is one
of the configured TICKERS, and 'off' otherwise (research watchlist / searched symbols).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.config import Settings, get_settings
from src.db.schema import PendingOrder, TickerMode, session_scope
from src.decision_engine.engine import Decision

MODES = ("off", "ask", "auto")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def default_mode(symbol: str, settings: Settings | None = None) -> str:
    s = settings or get_settings()
    return s.default_trade_mode if symbol.upper() in {t.upper() for t in s.tickers} else "off"


def get_modes(engine, symbols=None, settings: Settings | None = None) -> dict[str, str]:
    """Effective mode for each symbol (configured tickers by default, plus anything stored)."""
    s = settings or get_settings()
    with session_scope(engine) as sx:
        stored = {r.symbol: r.mode for r in sx.execute(select(TickerMode)).scalars()}
    syms = list(dict.fromkeys([*(symbols or s.tickers), *stored]))
    return {sym: stored.get(sym, default_mode(sym, s)) for sym in syms}


def set_mode(engine, symbol: str, mode: str) -> None:
    mode = mode.lower()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    symbol = symbol.upper()
    with session_scope(engine) as sx:
        row = sx.get(TickerMode, symbol)
        if row is None:
            sx.add(TickerMode(symbol=symbol, mode=mode, updated_at=_utcnow()))
        else:
            row.mode, row.updated_at = mode, _utcnow()


def active_symbols(engine, settings: Settings | None = None) -> dict[str, str]:
    """Symbols the scheduler must look at: everything not 'off'."""
    return {k: v for k, v in get_modes(engine, settings=settings).items() if v != "off"}


# ------------------------------------------------------------------ approvals
def create_pending(engine, d: Decision, signal_id: int | None, ttl_minutes: int | None = None,
                   settings: Settings | None = None, now: datetime | None = None) -> int | None:
    """Queue a decision for approval. Returns None if an identical pending request already exists."""
    s = settings or get_settings()
    now = now or _utcnow()
    with session_scope(engine) as sx:
        dup = sx.execute(select(PendingOrder).where(
            PendingOrder.symbol == d.symbol, PendingOrder.direction == d.direction,
            PendingOrder.signal_time == d.signal_time, PendingOrder.status == "pending")).scalars().first()
        if dup is not None:
            return None
        row = PendingOrder(
            created_at=now, expires_at=now + timedelta(minutes=ttl_minutes or s.approval_ttl_minutes),
            symbol=d.symbol, direction=d.direction, qty=d.qty, entry=d.entry, stop_loss=d.stop_loss,
            take_profit=d.take_profit, probability=d.probability, sentiment=d.sentiment_score,
            signal_id=signal_id, signal_time=d.signal_time, reasons_json=json.dumps(d.reasons)[:4000],
            status="pending",
        )
        sx.add(row)
        sx.flush()
        return row.id


def list_pending(engine, status: str | None = "pending") -> list[PendingOrder]:
    with session_scope(engine) as sx:
        q = select(PendingOrder).order_by(PendingOrder.created_at.desc())
        if status:
            q = q.where(PendingOrder.status == status)
        rows = list(sx.execute(q).scalars())
        sx.expunge_all()
    return rows


def decide(engine, pending_id: int, approve: bool, note: str = "", now: datetime | None = None) -> bool:
    """Approve / reject a pending order. Only a still-pending, unexpired row can be decided."""
    now = now or _utcnow()
    with session_scope(engine) as sx:
        row = sx.get(PendingOrder, pending_id)
        if row is None or row.status != "pending":
            return False
        if row.expires_at <= now:
            row.status, row.note = "expired", "expired before decision"
            return False
        row.status, row.decided_at, row.note = ("approved" if approve else "rejected"), now, note[:500] or None
        return True


def expire_stale(engine, now: datetime | None = None) -> int:
    now = now or _utcnow()
    n = 0
    with session_scope(engine) as sx:
        for row in sx.execute(select(PendingOrder).where(PendingOrder.status == "pending",
                                                         PendingOrder.expires_at <= now)).scalars():
            row.status, row.note = "expired", "not approved in time"
            n += 1
    return n


def mark(engine, pending_id: int, status: str, note: str = "", trade_id: int | None = None) -> None:
    with session_scope(engine) as sx:
        row = sx.get(PendingOrder, pending_id)
        if row is not None:
            row.status = status
            if note:
                row.note = note[:500]
            if trade_id:
                row.trade_id = trade_id


def decision_from_pending(p: PendingOrder) -> Decision:
    return Decision(
        symbol=p.symbol, trade=True, direction=p.direction, qty=p.qty, entry=p.entry, stop_loss=p.stop_loss,
        take_profit=p.take_profit, probability=p.probability, sentiment_score=p.sentiment,
        signal_time=p.signal_time, reasons=json.loads(p.reasons_json or "[]"),
    )
