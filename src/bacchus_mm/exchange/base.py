"""Exchange-agnostic types and adapter interface.

Prices are Decimal dollars in [0, 1] on the YES side of a binary market.
Counts are whole contracts (int). A signed position is in yes-equivalent
contracts: +10 means long 10 YES, -10 means long 10 NO.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import AsyncIterator, Callable, Optional


class Side(str, Enum):
    BID = "bid"  # buy yes
    ASK = "ask"  # sell yes / equivalent to buying no


@dataclass
class MarketInfo:
    ticker: str
    event_ticker: str
    title: str
    category: str
    close_time: str
    yes_bid: Optional[Decimal]
    yes_ask: Optional[Decimal]
    volume_24h: Decimal
    open_interest: Decimal
    series_ticker: str = ""
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def spread(self) -> Optional[Decimal]:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    @property
    def mid(self) -> Optional[Decimal]:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2


@dataclass
class Order:
    order_id: str
    client_order_id: str
    ticker: str
    side: Side
    price: Decimal
    count: int
    status: str = "resting"
    placed_monotonic: float = 0.0  # local clock at placement, for TTL-aware refresh


@dataclass
class Fill:
    trade_id: str
    order_id: str
    ticker: str
    # Signed yes-equivalent contract delta: >0 we got longer yes, <0 shorter.
    signed_count: int
    yes_price: Decimal
    is_taker: bool
    ts_ms: int
    raw: dict = field(repr=False, default_factory=dict)


@dataclass
class BookTop:
    """Best bid/ask on the yes side, derived from the full book."""

    ticker: str
    bid: Optional[Decimal]
    bid_size: int
    ask: Optional[Decimal]
    ask_size: int
    ts_ms: int

    @property
    def mid(self) -> Optional[Decimal]:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2


class ExchangeAdapter(abc.ABC):
    """Minimal surface a market-making strategy needs from an exchange.

    The Kalshi implementation is the only one today; a Polymarket adapter
    should be able to implement this without changes to strategy/risk code.
    """

    @abc.abstractmethod
    async def list_markets(self) -> list[MarketInfo]: ...

    @abc.abstractmethod
    async def create_order(
        self,
        ticker: str,
        side: Side,
        price: Decimal,
        count: int,
        client_order_id: str,
        expiration_seconds: Optional[int] = None,
    ) -> Order: ...

    @abc.abstractmethod
    async def cancel_order(self, order_id: str) -> None: ...

    @abc.abstractmethod
    async def cancel_all_orders(self, tickers: Optional[list[str]] = None) -> int: ...

    @abc.abstractmethod
    async def get_resting_orders(self) -> list[Order]: ...

    @abc.abstractmethod
    async def get_positions(self) -> dict[str, int]:
        """ticker -> signed yes-equivalent contracts"""

    @abc.abstractmethod
    async def get_balance(self) -> Decimal: ...

    @abc.abstractmethod
    async def stream(
        self,
        get_tickers: Callable[[], list[str]],
        on_book_top: Callable[[BookTop], None],
        on_fill: Callable[[Fill], None],
    ) -> AsyncIterator[None]:
        """Run the market-data + fills stream until cancelled. get_tickers is
        re-evaluated on every (re)connect so the subscription can change
        mid-session (see request_resubscribe on implementations)."""

    @abc.abstractmethod
    async def close(self) -> None: ...
