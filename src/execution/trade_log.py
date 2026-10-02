"""Trade logging and broker reconciliation.

Every order attempt, fill and rejection is written to `trades` as soon as the broker answers.
`reconcile()` runs at the start of each cycle and compares our books with the broker's.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select

from src.decision_engine.engine import Decision
from src.execution.base import Broker, OrderResult
from src.db.schema import Signal, Trade, session_scope


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _pnl(direction: str, entry: float, exit_: float, qty: float) -> float:
    return (exit_ - entry) * qty if direction == "long" else (entry - exit_) * qty


def record_order(engine, decision: Decision, order: OrderResult, mode: str, signal_id: int | None = None) -> int:
    """Insert a trade row for an order attempt. Returns the trade id."""
    if order.is_rejected or not order.id:
        status = "rejected"
    elif order.status == "filled":
        status = "filled"
    else:
        status = "open"
    with session_scope(engine) as s:
        if signal_id is None and decision.signal_time is not None:
            sg = s.execute(
                select(Signal).where(Signal.symbol == decision.symbol, Signal.timestamp == decision.signal_time)
            ).scalars().first()
            signal_id = sg.id if sg else None
        t = Trade(
            symbol=decision.symbol,
            entry_time=_utcnow() if status in {"filled", "open"} else None,
            direction=decision.direction or "long",
            entry_price=order.filled_avg_price or decision.entry,
            qty=float(order.filled_qty or decision.qty),
            signal_id=signal_id,
            model_probability=decision.probability,
            sentiment_at_entry=decision.sentiment_score,
            stop_loss=decision.stop_loss,
            take_profit=decision.take_profit,
            broker_order_id=order.id or None,
            mode=mode,
            status=status,
            note=(order.message or "; ".join(decision.reasons[-2:]))[:500],
        )
        s.add(t)
        s.flush()
        return t.id


def close_trade(engine, trade_id: int, exit_price: float, note: str = "") -> None:
    with session_scope(engine) as s:
        t = s.get(Trade, trade_id)
        if t is None or t.status == "closed":
            return
        t.exit_price, t.exit_time, t.status = float(exit_price), _utcnow(), "closed"
        if t.entry_price is not None:
            t.pnl = _pnl(t.direction, t.entry_price, float(exit_price), t.qty)
        if note:
            t.note = (note if not t.note else f"{t.note} | {note}")[:500]


def reconcile(engine, broker: Broker, notify: Callable[[str, str], object] | None = None) -> list[str]:
    """Compare DB trades with the broker; update fills/exits; return a list of issues found."""
    issues: list[str] = []
    positions = {p.symbol: p for p in broker.get_positions()}

    with session_scope(engine) as s:
        open_trades = list(s.execute(select(Trade).where(Trade.status.in_(["open", "filled"]))).scalars())
        snapshot = [(t.id, t.symbol, t.broker_order_id, t.status, t.direction, t.entry_price, t.qty, t.entry_time) for t in open_trades]

    tracked = set()
    for tid, sym, oid, status, direction, entry_px, qty, entry_time in snapshot:
        tracked.add(sym)
        order = broker.get_order(oid) if oid else None

        if order is not None and status == "open" and order.status == "filled":
            with session_scope(engine) as s:
                t = s.get(Trade, tid)
                t.status, t.entry_price = "filled", order.filled_avg_price or t.entry_price
                t.qty = order.filled_qty or t.qty
        elif order is not None and order.is_rejected and status == "open":
            with session_scope(engine) as s:
                s.get(Trade, tid).status = "rejected"
            continue

        if sym in positions:
            p = positions[sym]
            if abs(abs(p.qty) - qty) > 1e-6 and order is not None and order.status == "filled":
                issues.append(f"{sym}: broker qty {p.qty} != logged qty {qty}")
            continue

        # not at the broker any more -> a bracket leg (or a manual close) ended it
        if order is not None and order.status == "filled":
            leg = next((l for l in order.legs if l.status == "filled" and l.filled_avg_price), None)
            if leg:
                close_trade(engine, tid, leg.filled_avg_price, f"exit via {'stop' if _is_stop(leg) else 'target'} leg")
                continue
            got = broker.last_exit_fill(sym, direction, after=entry_time)
            if got:
                close_trade(engine, tid, got[0], f"exit via {got[1]} (from broker fills)")
                continue
            issues.append(f"{sym}: position gone at broker with no filled exit leg (closed manually?)")
            close_trade(engine, tid, entry_px or 0.0, "closed externally; exit price unknown")
        elif order is None and status in {"open", "filled"}:
            issues.append(f"{sym}: logged trade has no broker order and no position")

    for sym, p in positions.items():
        if sym not in tracked:
            issues.append(f"{sym}: broker holds {p.qty} but no open trade is logged (orphan position)")

    if issues and notify:
        notify("Reconciliation mismatch:\n" + "\n".join(issues), "warning")
    return issues


def _is_stop(leg: OrderResult) -> bool:
    return leg.order_type in {"stop", "stop_limit"}


def flatten_all(engine, broker: Broker, notify: Callable[[str, str], object] | None = None) -> int:
    """Close every position (session end). Updates trade rows with exit prices."""
    positions = broker.get_positions()
    prices = {p.symbol: p.market_price for p in positions}
    with session_scope(engine) as s:
        open_ids = {
            t.symbol: t.id
            for t in s.execute(select(Trade).where(Trade.status.in_(["open", "filled"]))).scalars()
        }
    results = broker.close_all_positions()
    exit_px = {r.symbol: r.filled_avg_price for r in results if r}
    n = 0
    for sym, tid in open_ids.items():
        px = exit_px.get(sym) or prices.get(sym)
        if px:
            close_trade(engine, tid, px, "flattened at session end")
            n += 1
    if notify and positions:
        notify(f"Flattened {len(positions)} position(s) at session end", "session")
    return n
