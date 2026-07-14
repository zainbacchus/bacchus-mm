"""Per-market quoting worker.

Book updates arrive via callback and mark the market dirty; a throttled loop
recomputes quotes at most once per `requote_min_interval` and reconciles the
resting orders (cancel/replace only when price moved beyond tolerance or size
changed). Everything it does is logged with the context that produced it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .eventlog import EventLog
from .exchange.base import BookTop, ExchangeAdapter, Order, Side
from .exchange.kalshi import new_client_order_id
from .risk import RiskManager
from .strategy.avellaneda_stoikov import StrategyParams, VolEstimator, compute_quotes

log = logging.getLogger(__name__)


@dataclass
class WorkerConfig:
    requote_min_interval: float = 1.0
    requote_tolerance: Decimal = Decimal("0.01")
    order_ttl_seconds: int = 900
    mid_mark_interval: float = 15.0


class MarketWorker:
    def __init__(
        self,
        ticker: str,
        exchange: ExchangeAdapter,
        strategy: StrategyParams,
        risk: RiskManager,
        events: EventLog,
        cfg: WorkerConfig,
        dry_run: bool = False,
    ):
        self.ticker = ticker
        self.exchange = exchange
        self.strategy = strategy
        self.risk = risk
        self.events = events
        self.cfg = cfg
        self.dry_run = dry_run
        self.vol = VolEstimator(strategy.sigma_halflife_seconds, strategy.sigma_floor)
        self.top: Optional[BookTop] = None
        self.bid_order: Optional[Order] = None
        self.ask_order: Optional[Order] = None
        self._dirty = asyncio.Event()
        self._last_requote = 0.0
        self._last_mid_mark = 0.0
        self._stopped = False

    # Called from the websocket consumer (same event loop).
    def on_book_top(self, top: BookTop) -> None:
        self.top = top
        mid = top.mid
        if mid is not None:
            self.vol.update(mid)
            self.risk.on_mid(self.ticker, mid)
            now = time.monotonic()
            if now - self._last_mid_mark >= self.cfg.mid_mark_interval:
                self.events.record_mid(self.ticker, mid, top.bid, top.ask)
                self._last_mid_mark = now
        self._dirty.set()

    def current_mid(self) -> Optional[Decimal]:
        return self.top.mid if self.top else None

    async def run(self) -> None:
        while not self._stopped:
            await self._dirty.wait()
            self._dirty.clear()
            wait = self.cfg.requote_min_interval - (time.monotonic() - self._last_requote)
            if wait > 0:
                await asyncio.sleep(wait)
            if self._stopped or self.risk.halted:
                continue
            try:
                await self._requote()
            except Exception as e:  # noqa: BLE001 — a worker error must not kill the bot
                log.exception("worker %s requote failed", self.ticker)
                self.events.emit("error", ticker=self.ticker, where="requote", error=str(e))
                await asyncio.sleep(2)
            self._last_requote = time.monotonic()

    async def _requote(self) -> None:
        top = self.top
        if top is None or top.mid is None:
            return
        mid = top.mid
        inventory = self.risk.markets.get(self.ticker)
        q = inventory.position if inventory else 0

        quotes = compute_quotes(
            mid=mid,
            inventory=q,
            max_inventory=self.risk.params.max_contracts_per_market,
            sigma=self.vol.sigma,
            p=self.strategy,
        )
        self.events.emit(
            "quote_decision",
            ticker=self.ticker,
            mid=mid,
            book_bid=top.bid,
            book_bid_size=top.bid_size,
            book_ask=top.ask,
            book_ask_size=top.ask_size,
            inventory=q,
            sigma=quotes.sigma,
            reservation=quotes.reservation,
            half_spread=quotes.half_spread,
            bid=quotes.bid,
            bid_size=quotes.bid_size,
            ask=quotes.ask,
            ask_size=quotes.ask_size,
            dry_run=self.dry_run,
        )
        self.bid_order = await self._reconcile(Side.BID, self.bid_order, quotes.bid, quotes.bid_size)
        self.ask_order = await self._reconcile(Side.ASK, self.ask_order, quotes.ask, quotes.ask_size)

    async def _reconcile(
        self, side: Side, existing: Optional[Order], price: Optional[Decimal], size: int
    ) -> Optional[Order]:
        if self.dry_run:
            return None
        keep = (
            existing is not None
            and price is not None
            and abs(existing.price - price) < self.cfg.requote_tolerance
            and existing.count == size
        )
        if keep:
            return existing
        if existing is not None:
            try:
                await self.exchange.cancel_order(existing.order_id)
                self.events.emit(
                    "order_canceled", ticker=self.ticker, side=side.value,
                    order_id=existing.order_id, price=existing.price,
                )
            except Exception as e:  # noqa: BLE001
                self.events.emit(
                    "error", ticker=self.ticker, where="cancel", side=side.value, error=str(e)
                )
        if price is None or size <= 0:
            return None
        signed = size if side is Side.BID else -size
        ok, reason = self.risk.approve_order(self.ticker, signed, price)
        if not ok:
            self.events.emit(
                "order_blocked", ticker=self.ticker, side=side.value,
                price=price, size=size, reason=reason,
            )
            return None
        try:
            order = await self.exchange.create_order(
                ticker=self.ticker,
                side=side,
                price=price,
                count=size,
                client_order_id=new_client_order_id(),
                expiration_seconds=self.cfg.order_ttl_seconds,
            )
            self.events.emit(
                "order_placed", ticker=self.ticker, side=side.value,
                order_id=order.order_id, price=price, size=size,
            )
            return order
        except Exception as e:  # noqa: BLE001
            self.events.emit(
                "order_rejected", ticker=self.ticker, side=side.value,
                price=price, size=size, error=str(e),
            )
            return None

    def order_filled(self, order_id: str, count: int) -> None:
        """Track a fill against our resting orders; forget fully-filled ones so the
        next reconcile re-places instead of cancelling a ghost."""
        for attr in ("bid_order", "ask_order"):
            order = getattr(self, attr)
            if order and order.order_id == order_id:
                order.count -= count
                if order.count <= 0:
                    setattr(self, attr, None)
        self._dirty.set()

    async def stop(self) -> None:
        self._stopped = True
        self._dirty.set()
        for order in (self.bid_order, self.ask_order):
            if order is not None and not self.dry_run:
                try:
                    await self.exchange.cancel_order(order.order_id)
                except Exception as e:  # noqa: BLE001
                    log.warning("stop-cancel %s failed: %s", order.order_id, e)
        self.bid_order = self.ask_order = None
