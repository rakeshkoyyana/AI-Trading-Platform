"""
Alpaca execution adapter (alpaca-py).

Safety:
  * paper trading unless TRADING_MODE=live AND LIVE_TRADING_CONFIRMED=true in the environment
  * the kill switch (KILL_SWITCH file) blocks every NEW order; closing orders are always allowed
  * bracket orders (stop + target) are attached at entry so risk is defined broker-side even if
    this process dies
"""
from __future__ import annotations

import os

from src.config import Settings, get_settings
from src.execution.base import AccountInfo, Broker, OrderResult, Position


def _result(o) -> OrderResult:
    legs = [_result(leg) for leg in (getattr(o, "legs", None) or [])]
    side = getattr(o.side, "value", str(o.side)).lower()
    status = getattr(o.status, "value", str(o.status)).lower()
    return OrderResult(
        id=str(o.id),
        symbol=o.symbol,
        side=side,
        qty=float(o.qty or 0),
        status=status,
        filled_qty=float(o.filled_qty or 0),
        filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None,
        legs=legs,
        order_type=getattr(getattr(o, "order_type", None), "value", str(getattr(o, "order_type", ""))).lower(),
    )


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(self, settings: Settings | None = None, client=None):
        self.settings = settings or get_settings()
        s = self.settings
        if s.is_live and os.getenv("LIVE_TRADING_CONFIRMED", "").lower() != "true":
            raise RuntimeError(
                "TRADING_MODE=live but LIVE_TRADING_CONFIRMED is not 'true'. Refusing to route live orders."
            )
        self.is_paper = not s.is_live
        if client is None:
            if not s.alpaca_api_key or not s.alpaca_secret_key:
                raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
            from alpaca.trading.client import TradingClient

            client = TradingClient(s.alpaca_api_key, s.alpaca_secret_key, paper=self.is_paper)
        self.client = client

    # ------------------------------------------------------------------ orders
    def place_order(self, symbol, side, qty, stop_loss=None, take_profit=None) -> OrderResult:
        if self.settings.kill_switch_active:
            return OrderResult("", symbol, side, qty, "rejected", message="kill switch active")
        if qty < 1:
            return OrderResult("", symbol, side, qty, "rejected", message="qty < 1")
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest

        kwargs = dict(
            symbol=symbol,
            qty=int(qty),
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        if stop_loss is not None and take_profit is not None:
            kwargs.update(
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=round(float(stop_loss), 2)),
                take_profit=TakeProfitRequest(limit_price=round(float(take_profit), 2)),
            )
        elif stop_loss is not None:  # stop-only: Pine rules do the exit, the stop is the safety net
            kwargs.update(
                order_class=OrderClass.OTO,
                stop_loss=StopLossRequest(stop_price=round(float(stop_loss), 2)),
            )
        try:
            return _result(self.client.submit_order(MarketOrderRequest(**kwargs)))
        except Exception as exc:  # noqa: BLE001 - broker rejections must be logged, not raised
            return OrderResult("", symbol, side, qty, "rejected", message=str(exc)[:300])

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel_order_by_id(order_id)
            return True
        except Exception:  # noqa: BLE001
            return False

    def get_order(self, order_id: str) -> OrderResult | None:
        try:
            from alpaca.trading.requests import GetOrderByIdRequest

            # nested=True returns the stop / target legs, which reconcile needs to see which one filled
            return _result(self.client.get_order_by_id(order_id, GetOrderByIdRequest(nested=True)))
        except Exception:  # noqa: BLE001
            return None

    _LIVE = {"new", "accepted", "held", "pending_new", "partially_filled", "pending_replace", "accepted_for_bidding"}

    def _open_orders(self, symbols=None) -> list[OrderResult]:
        """Every live order, bracket legs included, as one flat de-duplicated list.

        Alpaca rolls bracket legs up under their (already filled) parent when nested=True, and the parent is not
        'open' any more, so a nested-only query can miss a live stop. Worse, a bracket's STOP leg sits in status
        'held' until it is triggered, and Alpaca's 'open' filter does not list held orders at all: only the take-profit
        leg shows up. The held stop is only visible nested under its filled parent in an ALL-status query. So ask
        three ways (open flat, open nested, all nested) and merge."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        seen: dict[str, OrderResult] = {}
        for status, nested in ((QueryOrderStatus.OPEN, False), (QueryOrderStatus.OPEN, True), (QueryOrderStatus.ALL, True)):
            kw = dict(status=status, nested=nested, limit=500)
            if symbols:
                kw["symbols"] = list(symbols)
            for o in self.client.get_orders(GetOrdersRequest(**kw)):
                for x in [o, *(getattr(o, "legs", None) or [])]:
                    r = _result(x)
                    if r.status in self._LIVE:  # the ALL query also returns every finished order; keep only the live ones
                        seen.setdefault(r.id, r)
        return list(seen.values())

    def protected_symbols(self) -> set[str] | None:
        try:
            return {r.symbol for r in self._open_orders()
                    if r.order_type in {"stop", "stop_limit", "trailing_stop"} and r.status in self._LIVE}
        except Exception:  # noqa: BLE001
            return None

    def modify_exit_levels(self, symbol, stop, target):
        try:
            from alpaca.trading.requests import ReplaceOrderRequest

            pos = next((p for p in self.get_positions() if p.symbol == symbol and p.qty), None)
            if pos is None:
                return False, "no open position at the broker"
            exit_side = "sell" if pos.qty > 0 else "buy"
            stop_leg = tp_leg = None
            orders = self._open_orders([symbol])
            for r in orders:
                if r.symbol != symbol or r.side != exit_side or r.status not in self._LIVE:
                    continue
                if r.order_type in {"stop", "stop_limit"} and stop_leg is None:
                    stop_leg = r
                elif r.order_type == "limit" and tp_leg is None:
                    tp_leg = r
            msgs = []
            if stop is not None:
                if stop_leg is None:
                    seen = ", ".join(f"{r.side} {r.order_type} {r.status}" for r in orders if r.symbol == symbol) or "none"
                    return False, f"no resting stop order found at the broker (orders seen for {symbol}: {seen})"
                self.client.replace_order_by_id(stop_leg.id, ReplaceOrderRequest(stop_price=round(float(stop), 2)))
                msgs.append(f"stop -> {float(stop):.2f}")
            if target is not None:
                if tp_leg is None:
                    msgs.append("target NOT changed (this trade has no take-profit order)")
                else:
                    self.client.replace_order_by_id(tp_leg.id, ReplaceOrderRequest(limit_price=round(float(target), 2)))
                    msgs.append(f"target -> {float(target):.2f}")
            return True, "; ".join(msgs)
        except Exception as exc:  # noqa: BLE001
            return False, f"broker refused: {str(exc)[:200]}"

    def last_exit_fill(self, symbol, direction, after=None):
        try:
            from datetime import timezone

            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest

            want = "sell" if direction == "long" else "buy"
            if after is not None and getattr(after, "tzinfo", None) is None:
                after = after.replace(tzinfo=timezone.utc)
            orders = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[symbol],
                                                             after=after, limit=20, nested=True))
            fills = []
            for o in orders:
                for x in [o, *(getattr(o, "legs", None) or [])]:
                    r = _result(x)
                    if r.side == want and r.filled_avg_price and r.status == "filled":
                        fills.append((getattr(x, "filled_at", None), r))
            if not fills:
                return None
            fills.sort(key=lambda f: str(f[0]))
            r = fills[-1][1]
            kind = "stop" if r.order_type in {"stop", "stop_limit"} else ("target" if r.order_type == "limit" else "market")
            return float(r.filled_avg_price), kind
        except Exception:  # noqa: BLE001
            return None

    # ----------------------------------------------------------------- account
    def get_positions(self) -> list[Position]:
        return [
            Position(
                symbol=p.symbol,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                market_price=float(p.current_price) if p.current_price else None,
                unrealized_pnl=float(p.unrealized_pl) if p.unrealized_pl else None,
            )
            for p in self.client.get_all_positions()
        ]

    def get_account(self) -> AccountInfo:
        a = self.client.get_account()
        return AccountInfo(
            equity=float(a.equity),
            cash=float(a.cash),
            buying_power=float(a.buying_power),
            last_equity=float(a.last_equity) if getattr(a, "last_equity", None) else None,
        )

    def close_position(self, symbol: str) -> OrderResult | None:
        """Cancel the symbol's resting stop first (it holds the shares), then close at market."""
        import time

        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest

            for o in self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])):
                try:
                    self.client.cancel_order_by_id(o.id)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        for attempt in range(3):
            try:
                return _result(self.client.close_position(symbol))
            except Exception:  # noqa: BLE001 - cancel may still be settling
                time.sleep(0.5 * (attempt + 1))
        return None

    def close_all_positions(self) -> list[OrderResult]:
        try:
            self.client.cancel_orders()  # also cancels bracket legs
            self.client.close_all_positions(cancel_orders=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[alpaca] close_all failed: {exc}")
        return []


def get_broker(settings: Settings | None = None) -> Broker:
    """Factory: BROKER=sim returns the in-memory simulator; default is Alpaca."""
    s = settings or get_settings()
    if os.getenv("BROKER", "alpaca").lower() == "sim":
        from src.execution.sim_broker import SimBroker

        return SimBroker()
    return AlpacaBroker(s)
