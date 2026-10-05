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
from src.db.schema import CloseRequest, ModifyRequest, PendingOrder, RiskOverride, TickerMode, Trade, session_scope
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


# ------------------------------------------------------------ manual close
CLOSE_TTL_SECONDS = 120  # a click the scheduler never picked up must not fire later by surprise


def request_close(engine, symbol: str, now: datetime | None = None) -> int | None:
    """Queue a close for `symbol`. Returns None if one is already waiting."""
    symbol = symbol.upper()
    with session_scope(engine) as sx:
        if sx.execute(select(CloseRequest).where(CloseRequest.symbol == symbol,
                                                 CloseRequest.status == "pending")).scalars().first():
            return None
        row = CloseRequest(created_at=now or _utcnow(), symbol=symbol, status="pending")
        sx.add(row)
        sx.flush()
        return row.id


def list_close_requests(engine, status: str | None = "pending") -> list[CloseRequest]:
    with session_scope(engine) as sx:
        q = select(CloseRequest).order_by(CloseRequest.created_at.desc())
        if status:
            q = q.where(CloseRequest.status == status)
        rows = list(sx.execute(q).scalars())
        sx.expunge_all()
    return rows


def mark_close(engine, req_id: int, status: str, note: str = "") -> None:
    with session_scope(engine) as sx:
        row = sx.get(CloseRequest, req_id)
        if row is not None:
            row.status, row.note = status, (note or None)


def expire_close_requests(engine, now: datetime | None = None) -> int:
    cutoff = (now or _utcnow()) - timedelta(seconds=CLOSE_TTL_SECONDS)
    n = 0
    with session_scope(engine) as sx:
        for row in sx.execute(select(CloseRequest).where(CloseRequest.status == "pending",
                                                         CloseRequest.created_at <= cutoff)).scalars():
            row.status, row.note = "expired", "scheduler did not pick it up in time (is it running?)"
            n += 1
    return n


def open_positions(engine) -> list[dict]:
    """Trades the platform believes are open (for the dashboard's Close buttons)."""
    with session_scope(engine) as sx:
        rows = sx.execute(select(Trade).where(Trade.status.in_(["open", "filled"])).order_by(Trade.id)).scalars()
        return [dict(symbol=t.symbol, direction=t.direction, qty=t.qty, entry=t.entry_price,
                     stop=t.stop_loss, target=t.take_profit) for t in rows]


def change_signature(engine) -> tuple:
    """Cheap fingerprint of everything the dashboard shows that other processes change (trades, approvals, closes).
    The dashboard polls it every few seconds and redraws the whole page the moment it differs."""
    from sqlalchemy import func

    with session_scope(engine) as sx:
        trades = sx.execute(select(Trade.id, Trade.status, Trade.exit_price)).all()
        pend = sx.execute(select(PendingOrder.id, PendingOrder.status)).all()
        closes = sx.execute(select(CloseRequest.id, CloseRequest.status)).all()
        modes = sx.execute(select(TickerMode.symbol, TickerMode.mode)).all()
    with session_scope(engine) as sx:
        mods = sx.execute(select(ModifyRequest.id, ModifyRequest.status)).all()
        levels = sx.execute(select(PendingOrder.id, PendingOrder.stop_loss, PendingOrder.take_profit)).all()
        tlevels = sx.execute(select(Trade.id, Trade.stop_loss, Trade.take_profit)).all()
    return (tuple(map(tuple, trades)), tuple(map(tuple, pend)), tuple(map(tuple, closes)), tuple(map(tuple, modes)),
            tuple(map(tuple, mods)), tuple(map(tuple, levels)), tuple(map(tuple, tlevels)))


# ------------------------------------------------------- draggable SL / TP
MAX_WIDEN = 2.0  # a stop may be dragged at most this many times farther than the engine's original stop (tightening is always fine)
MIN_STOP_PCT = 0.0005  # and never closer than 0.05% of price (it would trigger on noise)


def validate_levels(direction: str, ref: float, stop: float | None, target: float | None, orig_stop: float | None,
                    settings: Settings | None = None) -> tuple[bool, str]:
    """Is (stop, target) a sane pair for a `direction` trade around reference price `ref`?"""
    s = settings or get_settings()
    long = direction == "long"
    if ref is None or ref <= 0:
        return False, "no reference price"
    if stop is not None:
        if (long and stop >= ref) or (not long and stop <= ref):
            return False, f"stop {stop:.2f} must be {'below' if long else 'above'} {ref:.2f} for a {direction}"
        dist = abs(ref - stop)
        if dist / ref > s.max_stop_pct:
            return False, f"stop {dist / ref:.1%} away is wider than the {s.max_stop_pct:.0%} limit"
        if dist / ref < MIN_STOP_PCT:
            return False, "stop is too close to the price"
        if orig_stop is not None and abs(ref - orig_stop) > 0 and dist > MAX_WIDEN * abs(ref - orig_stop):
            return False, f"stop can be widened to at most {MAX_WIDEN:g}x its original distance"
    if target is not None and ((long and target <= ref) or (not long and target >= ref)):
        return False, f"target {target:.2f} must be {'above' if long else 'below'} {ref:.2f} for a {direction}"
    return True, ""


def update_pending_levels(engine, pending_id: int, stop: float | None, target: float | None,
                          settings: Settings | None = None, now: datetime | None = None) -> tuple[bool, str]:
    """Change the stop / target of a still-pending proposal (what Approve will then send)."""
    now = now or _utcnow()
    with session_scope(engine) as sx:
        row = sx.get(PendingOrder, pending_id)
        if row is None or row.status != "pending" or row.expires_at <= now:
            return False, "that request is no longer waiting for approval"
        ok, why = validate_levels(row.direction, row.entry, stop, target if row.take_profit else None, row.stop_loss, settings)
        if not ok:
            return False, why
        if stop is not None:
            row.stop_loss = round(float(stop), 2)
        if target is not None and row.take_profit is not None:
            row.take_profit = round(float(target), 2)
        return True, ""


def request_modify(engine, symbol: str, stop: float | None, target: float | None, now: datetime | None = None,
                   settings: Settings | None = None) -> tuple[int | None, str]:
    """Queue new stop / target for an open position (validated now and again when the scheduler applies it)."""
    symbol = symbol.upper()
    with session_scope(engine) as sx:
        t = sx.execute(select(Trade).where(Trade.symbol == symbol, Trade.status.in_(["open", "filled"]))
                       .order_by(Trade.id.desc())).scalars().first()
        if t is None:
            return None, "no open position for this ticker"
        ref = t.entry_price
        ok, why = validate_levels(t.direction, ref, stop, target if t.take_profit else None, t.stop_loss, settings)
        if not ok:
            return None, why
        row = ModifyRequest(created_at=now or _utcnow(), symbol=symbol, stop=stop, target=target, status="pending")
        sx.add(row)
        sx.flush()
        return row.id, ""


def list_modify_requests(engine, status: str | None = "pending") -> list[ModifyRequest]:
    with session_scope(engine) as sx:
        q = select(ModifyRequest).order_by(ModifyRequest.created_at.desc())
        if status:
            q = q.where(ModifyRequest.status == status)
        rows = list(sx.execute(q).scalars())
        sx.expunge_all()
    return rows


def mark_modify(engine, req_id: int, status: str, note: str = "") -> None:
    with session_scope(engine) as sx:
        row = sx.get(ModifyRequest, req_id)
        if row is not None:
            row.status, row.note = status, (note or None)


def expire_modify_requests(engine, now: datetime | None = None) -> int:
    cutoff = (now or _utcnow()) - timedelta(seconds=CLOSE_TTL_SECONDS)
    n = 0
    with session_scope(engine) as sx:
        for row in sx.execute(select(ModifyRequest).where(ModifyRequest.status == "pending",
                                                          ModifyRequest.created_at <= cutoff)).scalars():
            row.status, row.note = "expired", "scheduler did not pick it up in time (is it running?)"
            n += 1
    return n


def chart_levels(engine, symbol: str, now: datetime | None = None) -> dict:
    """What the chart draws for `symbol`: proposals waiting for approval and open positions, with their stop / target."""
    now = now or _utcnow()
    symbol = symbol.upper()
    pend = [dict(id=p.id, direction=p.direction, qty=p.qty, entry=p.entry, stop=p.stop_loss, target=p.take_profit,
                 expires=p.expires_at.isoformat() if p.expires_at else None)
            for p in list_pending(engine, "pending") if p.symbol == symbol and p.expires_at > now and p.entry]
    waiting = {m.symbol for m in list_modify_requests(engine, "pending")}
    with session_scope(engine) as sx:
        rows = list(sx.execute(select(Trade).where(Trade.symbol == symbol, Trade.status.in_(["open", "filled"]))).scalars())
        rr = get_settings().target_rr
        pos = [dict(id=t.id, direction=t.direction, qty=t.qty, entry=t.entry_price, stop=t.stop_loss, target=t.take_profit,
                    # a trade opened without a take-profit order still shows where a 1:RR target would sit (not an order)
                    target_est=(None if t.take_profit or not t.stop_loss else
                                round(t.entry_price + (1 if t.direction == "long" else -1) * rr * abs(t.entry_price - t.stop_loss), 2)),
                    t0=int(t.entry_time.replace(tzinfo=timezone.utc).timestamp()) if t.entry_time else None,
                    updating=symbol in waiting) for t in rows if t.entry_price]
    return dict(pending=pend, positions=pos)


# ------------------------------------------------------------ position sizing (editable on the dashboard)
RISK_KEYS = ("max_position_pct", "risk_per_trade_pct")
RISK_LIMITS = {"max_position_pct": (0.005, 0.25), "risk_per_trade_pct": (0.0005, 0.02)}  # 0.5%-25% and 0.05%-2%


def get_risk(engine, settings: Settings | None = None) -> dict[str, float]:
    """Current sizing limits: the dashboard's saved values, else the .env defaults."""
    s = settings or get_settings()
    out = {k: float(getattr(s, k)) for k in RISK_KEYS}
    with session_scope(engine) as sx:
        for row in sx.execute(select(RiskOverride)).scalars():
            if row.key in out:
                out[row.key] = float(row.value)
    return out


def set_risk(engine, max_position_pct: float, risk_per_trade_pct: float, now: datetime | None = None) -> tuple[bool, str]:
    """Save new sizing limits (fractions of equity, e.g. 0.05 = 5%). Applies to signals from the next cycle on."""
    vals = {"max_position_pct": float(max_position_pct), "risk_per_trade_pct": float(risk_per_trade_pct)}
    for k, v in vals.items():
        lo, hi = RISK_LIMITS[k]
        if not lo <= v <= hi:
            return False, f"{k.replace('_', ' ')} must be between {lo:.2%} and {hi:.0%}"
    with session_scope(engine) as sx:
        for k, v in vals.items():
            row = sx.get(RiskOverride, k)
            if row is None:
                sx.add(RiskOverride(key=k, value=v, updated_at=now or _utcnow()))
            else:
                row.value, row.updated_at = v, now or _utcnow()
    return True, ""


def reset_risk(engine) -> None:
    with session_scope(engine) as sx:
        for row in sx.execute(select(RiskOverride)).scalars():
            sx.delete(row)


def effective_settings(engine, settings: Settings | None = None) -> Settings:
    """Settings with the dashboard's sizing limits applied (what the decision engine should use)."""
    import dataclasses

    s = settings or get_settings()
    try:
        return dataclasses.replace(s, **get_risk(engine, s))
    except Exception:  # noqa: BLE001  (an old database without the table must never stop trading)
        return s


MAX_QTY_FACTOR = 3  # one edit may raise a proposal's share count by at most this multiple; the broker still enforces buying power


def update_pending_qty(engine, pending_id: int, qty: int, now: datetime | None = None) -> tuple[bool, str]:
    """Change how many shares a still-pending proposal will buy / sell when approved."""
    now = now or _utcnow()
    with session_scope(engine) as sx:
        row = sx.get(PendingOrder, pending_id)
        if row is None or row.status != "pending" or row.expires_at <= now:
            return False, "that request is no longer waiting for approval"
        qty = int(qty)
        if qty < 1:
            return False, "at least 1 share"
        if qty > row.qty * MAX_QTY_FACTOR:
            return False, f"at most {row.qty * MAX_QTY_FACTOR} shares in one edit ({MAX_QTY_FACTOR}x the current {row.qty})"
        row.qty = qty
        return True, ""
