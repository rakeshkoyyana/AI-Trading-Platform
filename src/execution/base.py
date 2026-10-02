"""Broker-agnostic execution interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class OrderResult:
    id: str
    symbol: str
    side: str  # buy | sell
    qty: float
    status: str  # accepted | filled | partially_filled | rejected | cancelled | expired | new ...
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    legs: list["OrderResult"] = field(default_factory=list)
    message: str = ""
    order_type: str = ""  # market | limit | stop ... (bracket legs: stop = stop-loss, limit = take-profit)

    @property
    def is_rejected(self) -> bool:
        return self.status in {"rejected", "canceled", "cancelled", "expired"}


@dataclass
class Position:
    symbol: str
    qty: float  # positive long, negative short
    avg_entry_price: float
    market_price: float | None = None
    unrealized_pnl: float | None = None

    @property
    def direction(self) -> str:
        return "long" if self.qty > 0 else "short"


@dataclass
class AccountInfo:
    equity: float
    cash: float
    buying_power: float
    last_equity: float | None = None  # previous close equity (Alpaca provides it)


class Broker(ABC):
    """Everything the trading loop needs from a broker. Alpaca and the simulator implement it."""

    name = "base"
    is_paper = True

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> OrderResult:
        """Market order, with an attached stop/target bracket when both are given."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    def get_order(self, order_id: str) -> OrderResult | None: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_account(self) -> AccountInfo: ...

    @abstractmethod
    def close_position(self, symbol: str) -> OrderResult | None: ...

    def protected_symbols(self) -> set[str] | None:
        """Symbols that currently have a resting stop order at the broker (None = this broker cannot tell)."""
        return None

    def modify_exit_levels(self, symbol: str, stop: float | None, target: float | None) -> tuple[bool, str]:
        """Move the resting stop / take-profit orders of an open position. Returns (ok, message)."""
        return False, "this broker cannot modify exit orders"

    def last_exit_fill(self, symbol: str, direction: str, after=None) -> tuple[float, str] | None:
        """(price, kind) of the latest filled exit order for a position, kind in {'stop','target','market'}; None if unknown."""
        return None

    def get_account_equity(self) -> float:
        return self.get_account().equity

    def close_all_positions(self) -> list[OrderResult]:
        out = []
        for p in self.get_positions():
            r = self.close_position(p.symbol)
            if r:
                out.append(r)
        return out
