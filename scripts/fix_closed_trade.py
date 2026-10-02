#!/usr/bin/env python3
"""One-off repair: set a trade's exit price/P&L from the broker's actual closing fill.

    python scripts/fix_closed_trade.py 1            # trade id from the dashboard's Trade log
Looks up the most recent filled order on the opposite side after the trade's entry and books the trade as closed at that fill.
"""
from __future__ import annotations

import sys
from datetime import timezone

from sqlalchemy import select

from src.db.schema import Trade, get_engine, init_db, session_scope
from src.execution.alpaca_execution import AlpacaBroker
from src.execution.trade_log import _pnl


def main(trade_id: int) -> int:
    engine = get_engine()
    init_db(engine)
    with session_scope(engine) as s:
        t = s.get(Trade, trade_id)
        if t is None:
            print(f"no trade {trade_id}")
            return 1
        sym, direction, entry_time, entry_px, qty = t.symbol, t.direction, t.entry_time, t.entry_price, t.qty
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    broker = AlpacaBroker()
    want = "sell" if direction == "long" else "buy"
    after = entry_time.replace(tzinfo=timezone.utc) if entry_time else None
    orders = broker.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[sym], after=after, limit=20))
    fills = [o for o in orders if o.filled_avg_price and getattr(o.side, "value", str(o.side)).lower() == want]
    if not fills:
        print("no filled closing order found at the broker")
        return 1
    o = max(fills, key=lambda x: x.filled_at)
    px = float(o.filled_avg_price)
    with session_scope(engine) as s:
        t = s.get(Trade, trade_id)
        t.exit_price, t.status = px, "closed"
        t.exit_time = o.filled_at.astimezone(timezone.utc).replace(tzinfo=None)
        t.pnl = _pnl(direction, entry_px, px, qty)
        t.note = ((t.note or "") + " | exit price repaired from broker fill")[:500]
        print(f"{sym} {direction} x{qty}: entry {entry_px} -> exit {px:.2f}, P&L {t.pnl:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1])))
