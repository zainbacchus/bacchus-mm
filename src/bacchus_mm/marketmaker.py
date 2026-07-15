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
from collections import deque
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
    fast_move_threshold: Decimal = Decimal("0.03")
    fast_move_window: float = 30.0
    fast_move_cooloff: float = 180.0


class FastMoveGuard:
    """Regime-shift circuit breaker. The EWMA sigma reacts over minutes; a
    sudden repricing (data release, news) picks off resting quotes long before
    sigma widens them. If the mid moves >= threshold within the window, block
    quoting for a cool-off period — stand aside, don't catch the knife.

    Motivated by the first live fills (2026-07-15): a bid filled at $0.40
    seconds into a collapse to $0.14 accounted for all realized losses."""

    def __init__(self, threshold: Decimal, window_s: float, cooloff_s: float):
        self.threshold = threshold
        self.window = window_s
        self.cooloff = cooloff_s
        self._hist: deque[tuple[float, Decimal]] = deque()
        self._blocked_until = 0.0

    def update(self, mid: Decimal, ts: Optional[float] = None) -> None:
        now = ts if ts is not None else time.monotonic()
        self._hist.append((now, mid))
        while self._hist and now - self._hist[0][0] > self.window:
            self._hist.popleft()
        if abs(mid - self._hist[0][1]) >= self.threshold:
            self._blocked_until = now + self.cooloff

    def blocked(self, ts: Optional[float] = None) -> bool:
        now = ts if ts is not None else time.monotonic()
        return now < self._blocked_until


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
        self.guard = FastMoveGuard(cfg.fast_move_threshold, cfg.fast_move_window, cfg.fast_move_cooloff)
        self._guard_announced = False
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
            self.guard.update(mid)
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
            # Wake on book changes, but also tick periodically so TTL-refresh
            # happens even when a market goes completely silent.
            try:
                await asyncio.wait_for(self._dirty.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
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
        if self.guard.blocked():
            if not self._guard_announced:
                self.events.emit(
                    "quotes_pulled", ticker=self.ticker, reason="fast_move",
                    mid=top.mid, cooloff=self.cfg.fast_move_cooloff,
                )
                self._guard_announced = True
            self.bid_order = await self._reconcile(Side.BID, self.bid_order, None, 0)
            self.ask_order = await self._reconcile(Side.ASK, self.ask_order, None, 0)
            self._dirty.set()  # keep ticking so we resume when the cool-off ends
            return
        self._guard_announced = False
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
        # Refresh before the exchange-side TTL kills the order: past ~80% of its
        # life we re-place even at an unchanged price, otherwise a quiet market
        # leaves us holding a reference to an expired order and quoting nothing.
        fresh = (
            existing is not None
            and time.monotonic() - existing.placed_monotonic < self.cfg.order_ttl_seconds * 0.8
        )
        keep = (
            fresh
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
            order.placed_monotonic = time.monotonic()
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
