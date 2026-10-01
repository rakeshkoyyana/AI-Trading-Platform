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
            return _result(self.client.get_order_by_id(order_id))
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
        try:
            return _result(self.client.close_position(symbol))
        except Exception:  # noqa: BLE001
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
