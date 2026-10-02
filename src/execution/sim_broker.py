"""
In-memory simulated broker.

Used for (a) unit tests, (b) offline end-to-end replays through the real decision engine,
(c) `BROKER=sim` dry runs. Market orders fill at the price you set with `set_price`; bracket
legs are evaluated against each bar's high/low via `on_bar` (stop first if both touch —
the same conservative rule as the labeler).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

from src.config import get_settings
from src.execution.base import AccountInfo, Broker, OrderResult, Position


@dataclass
class _Bracket:
    parent_id: str
    symbol: str
    qty: int
    direction: str
    stop: float
    target: float | None
    stop_id: str
    target_id: str


class SimBroker(Broker):
    name = "sim"

    def __init__(self, equity: float = 100_000.0, slippage_bps: float = 1.0, fee_per_share: float = 0.0):
        self.cash = equity
        self.start_equity = equity
        self.last_equity = equity
        self.slip = slippage_bps / 10_000.0
        self.fee = fee_per_share
        self.prices: dict[str, float] = {}
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, OrderResult] = {}
        self.brackets: list[_Bracket] = []
        self.realized: list[dict] = []
        self._ids = itertools.count(1)

    # ---------------------------------------------------------------- helpers
    def set_price(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    def _mark(self, symbol: str) -> float:
        return self.prices[symbol]

    def _apply_fill(self, symbol: str, signed_qty: float, price: float) -> None:
        self.cash -= signed_qty * price + self.fee * abs(signed_qty)
        pos = self.positions.get(symbol)
        if pos is None:
            self.positions[symbol] = Position(symbol, signed_qty, price)
            return
        new_qty = pos.qty + signed_qty
        if abs(new_qty) < 1e-9:
            self.realized.append(
                dict(symbol=symbol, direction=pos.direction, qty=abs(pos.qty),
                     entry=pos.avg_entry_price, exit=price,
                     pnl=(price - pos.avg_entry_price) * pos.qty)
            )
            del self.positions[symbol]
        elif pos.qty * signed_qty > 0:  # adding
            pos.avg_entry_price = (pos.avg_entry_price * pos.qty + price * signed_qty) / new_qty
            pos.qty = new_qty
        else:
            pos.qty = new_qty

    # ------------------------------------------------------------------ Broker
    def place_order(self, symbol, side, qty, stop_loss=None, take_profit=None) -> OrderResult:
        if get_settings().kill_switch_active:
            return OrderResult("", symbol, side, qty, "rejected", message="kill switch active")
        if qty < 1 or symbol not in self.prices:
            return OrderResult("", symbol, side, qty, "rejected", message="bad qty or no price")
        px = self._mark(symbol)
        fill = px * (1 + self.slip) if side == "buy" else px * (1 - self.slip)
        signed = qty if side == "buy" else -qty
        if side == "buy" and fill * qty > self.cash + sum(
            abs(p.qty) * self.prices.get(p.symbol, p.avg_entry_price) for p in self.positions.values()
        ):
            return OrderResult("", symbol, side, qty, "rejected", message="insufficient buying power")
        self._apply_fill(symbol, signed, fill)
        oid = f"sim-{next(self._ids)}"
        res = OrderResult(oid, symbol, side, qty, "filled", qty, fill)
        if stop_loss is not None:
            sid, tid = f"sim-{next(self._ids)}", f"sim-{next(self._ids)}"
            exit_side = "sell" if side == "buy" else "buy"
            res.legs = [OrderResult(sid, symbol, exit_side, qty, "new", order_type="stop")]
            if take_profit is not None:
                res.legs.append(OrderResult(tid, symbol, exit_side, qty, "new", order_type="limit"))
            self.brackets.append(
                _Bracket(oid, symbol, qty, "long" if side == "buy" else "short",
                         float(stop_loss), None if take_profit is None else float(take_profit), sid, tid)
            )
        self.orders[oid] = res
        return res

    def on_bar(self, symbol: str, high: float, low: float, close: float) -> list[OrderResult]:
        """Advance the market: trigger bracket legs, then mark to close."""
        fills = []
        for b in list(self.brackets):
            if b.symbol != symbol or symbol not in self.positions:
                continue
            long = b.direction == "long"
            hit_stop = low <= b.stop if long else high >= b.stop
            hit_tgt = False if b.target is None else (high >= b.target if long else low <= b.target)
            if not (hit_stop or hit_tgt):
                continue
            px, leg_id, other = (b.stop, b.stop_id, b.target_id) if hit_stop else (b.target, b.target_id, b.stop_id)
            signed = -b.qty if long else b.qty
            self._apply_fill(symbol, signed, px)
            parent = self.orders[b.parent_id]
            for leg in parent.legs:
                if leg.id == leg_id:
                    leg.status, leg.filled_qty, leg.filled_avg_price = "filled", b.qty, px
                elif leg.id == other:
                    leg.status = "canceled"
            self.brackets.remove(b)
            fills.append(parent)
        self.prices[symbol] = close
        return fills

    def cancel_order(self, order_id: str) -> bool:
        return self.orders.pop(order_id, None) is not None

    def get_order(self, order_id: str) -> OrderResult | None:
        return self.orders.get(order_id)

    def get_positions(self) -> list[Position]:
        out = []
        for p in self.positions.values():
            mp = self.prices.get(p.symbol, p.avg_entry_price)
            out.append(Position(p.symbol, p.qty, p.avg_entry_price, mp, (mp - p.avg_entry_price) * p.qty))
        return out

    def get_account(self) -> AccountInfo:
        mkt = sum(p.qty * self.prices.get(p.symbol, p.avg_entry_price) for p in self.positions.values())
        equity = self.cash + mkt
        return AccountInfo(equity=equity, cash=self.cash, buying_power=max(0.0, self.cash) * 2, last_equity=self.last_equity)

    def close_position(self, symbol: str) -> OrderResult | None:
        p = self.positions.get(symbol)
        if p is None:
            return None
        self.brackets = [b for b in self.brackets if b.symbol != symbol]
        side = "sell" if p.qty > 0 else "buy"
        px = self._mark(symbol) * (1 - self.slip if side == "sell" else 1 + self.slip)
        qty = abs(p.qty)
        self._apply_fill(symbol, -p.qty, px)
        oid = f"sim-{next(self._ids)}"
        res = OrderResult(oid, symbol, side, qty, "filled", qty, px)
        self.orders[oid] = res
        return res

    def start_new_day(self) -> None:
        self.last_equity = self.get_account().equity
