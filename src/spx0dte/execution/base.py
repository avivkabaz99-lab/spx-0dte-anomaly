"""Execution contract.

Only `NullBroker` exists today: it records intent and places no orders, which
keeps the IBKR connection read-only. `PaperBroker` and `IBKRBroker` implement
the same Protocol later without changing any calling code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class Order:
    """An intent to trade. Immutable; brokers return fills separately."""

    contract_key: str
    side: Side
    qty: int
    limit_price: float
    signal_id: int | None = None


@dataclass(frozen=True)
class Fill:
    order: Order
    price: float
    ts: datetime


class BrokerInterface(Protocol):
    """Every execution backend implements this."""

    def place(self, order: Order) -> Fill | None:
        """Submit an order. Returns the fill, or None if nothing was executed."""
        ...

    def is_read_only(self) -> bool:
        """True if this backend can never reach a live venue."""
        ...


class NullBroker:
    """Records orders without executing them. The default, and read-only."""

    def __init__(self) -> None:
        self.submitted: list[Order] = []

    def place(self, order: Order) -> Fill | None:
        self.submitted.append(order)
        return None

    def is_read_only(self) -> bool:
        return True
