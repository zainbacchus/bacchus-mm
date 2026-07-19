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
from .strategy.avellaneda_stoikov import (
    StrategyParams,
    VolEstimator,
    apply_join_best,
    compute_quotes,
)

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
    # 2026-07-17 (H6): effective trip threshold = max(threshold, multiple x book
    # spread) and a 1x move must confirm across N same-direction updates — the
    # hair trigger fired 266x/12 evictions in 4 days, mostly one pulled level
    # moving a wide book's mid (KXRAINNYCM alone: 65+34 pulls).
    fast_move_spread_multiple: Decimal = Decimal("0.75")
    fast_move_confirm_updates: int = 2
    # A market that keeps tripping the guard is too fast for us, full stop:
    # evict it for the rest of the session instead of cycling pull/resume.
    # (2026-07-15: gas-CPI tripped 51x in 2.9h — 2.5h of cool-off churn.)
    guard_evict_trips: int = 8
    # 2026-07-17 (M4): wind-down distress alerting. A reduce-only exit that
    # stays unfilled for winddown_alert_seconds, or whose mid moves against the
    # position by >= winddown_alert_move from the wind-down anchor, emits a
    # loud winddown_distress event (repeating at most every
    # winddown_distress_repeat_seconds per market). The 07-16 failure mode: a
    # short ran 24c with a passive post-only exit resting forever.
    winddown_alert_seconds: float = 1800.0
    winddown_alert_move: Decimal = Decimal("0.05")
    winddown_distress_repeat_seconds: float = 900.0
    # 2026-07-17 (M4): wind-down exit escalation. "none" (default) keeps the
    # post-only invariant absolute. "cross_1tick" lets a DISTRESSED wind-down
    # exit (and nothing else) cross one tick as a taker. FLIPPING THIS IS AN
    # EXPLICIT OWNER DECISION that weakens the post-only invariant — even then
    # the path still gates through approve_order and only crosses when the
    # taker fee is smaller than the adverse move already suffered.
    winddown_escalation: str = "none"  # none | cross_1tick
    # 2026-07-17 (M6): after an ambiguous create failure (timeout/5xx — the
    # order may be live exchange-side), wait this long, then query resting
    # orders by client_order_id: adopt it if found, re-place if confirmed
    # lost. Never blind-retry a create (see kalshi.py create_order).
    create_adopt_delay: float = 2.0
    # 2026-07-18 (round 2): a single empty adopt-lookup does NOT prove the order
    # never landed — Kalshi's resting-orders query is eventually consistent, so
    # a just-landed order can be briefly invisible. Require this many CONSECUTIVE
    # empty lookups (each after another adopt_delay) before declaring the create
    # lost and re-placing; otherwise a lagging order + a fresh place = the same
    # quote on the book twice, both fillable.
    create_confirm_lost_lookups: int = 3


class FastMoveGuard:
    """Regime-shift circuit breaker. The EWMA sigma reacts over minutes; a
    sudden repricing (data release, news) picks off resting quotes long before
    sigma widens them. Trip -> block quoting for a cool-off period.

    2026-07-17 (H6): spread-scaled threshold + persistence confirmation.
    2026-07-17 review round 2 (adversarial): three defects fixed —
      - the windowed CUMULATIVE move detects multi-step collapses again (the
        rewrite only looked at single steps; a collapse that walks down 1-2c
        at a time — the shape of the motivating 0.40->0.14 incident — never
        tripped);
      - the effective threshold scales with the spread that PREVAILED before
        the move (min over the window, capped at 2x base): a shock blows the
        spread out at the moment of the move, which raised the bar exactly
        when it needed to hold; a single step >= 2x the BASE threshold always
        trips regardless of spread;
      - trips latch the PRE-move reference (trip_ref): persistence scoring
        must confirm moves that STICK (|now - ref| stays large) and forgive
        moves that revert — the previous code stored the post-move mid, which
        inverted the classification."""

    def __init__(
        self,
        threshold: Decimal,
        window_s: float,
        cooloff_s: float,
        spread_multiple: Decimal = Decimal("0.75"),
        confirm_updates: int = 2,
    ):
        self.threshold = threshold
        self.window = window_s
        self.cooloff = cooloff_s
        self.spread_multiple = spread_multiple
        self.confirm_updates = confirm_updates
        self._hist: deque[tuple[float, Decimal]] = deque()
        self._spread_hist: deque[tuple[float, Decimal]] = deque()
        self._blocked_until = 0.0
        # Pending (unconfirmed) move: direction, pre-move reference, steps, opened-at.
        self._pend_dir = 0
        self._pend_ref: Optional[Decimal] = None
        self._pend_steps = 0
        self._pend_opened = 0.0
        # Last trip's context, consumed by the worker at cool-off end.
        self.trip_seq = 0
        self.trip_ref: Optional[Decimal] = None  # PRE-move reference mid
        self.trip_mid: Optional[Decimal] = None  # mid at trip time
        self.trip_eff: Optional[Decimal] = None

    def _effective_threshold(self, now: float) -> Decimal:
        """Scale by the spread that prevailed BEFORE this update (min over the
        window — a shock's own blown-out spread must not raise the bar), and
        cap at 2x the base threshold so wide books still have a working guard."""
        while self._spread_hist and now - self._spread_hist[0][0] > self.window:
            self._spread_hist.popleft()
        if not self._spread_hist:
            return self.threshold
        prevailing = min(sp for _, sp in self._spread_hist)
        eff = max(self.threshold, self.spread_multiple * prevailing)
        return min(eff, 2 * self.threshold)

    def _clear_pending(self) -> None:
        self._pend_dir, self._pend_ref, self._pend_steps = 0, None, 0

    def _trip(self, now: float, ref: Decimal, mid: Decimal, eff: Decimal) -> None:
        if now < self._blocked_until:
            # Already in cool-off: continued movement (or the bounce back)
            # EXTENDS the block but must not overwrite the original trip
            # context — re-latching the reference mid-episode corrupts the
            # persistence scoring at cool-off end (guard_inversion_repro).
            self._blocked_until = max(self._blocked_until, now + self.cooloff)
            self._clear_pending()
            return
        self._blocked_until = now + self.cooloff
        self.trip_seq += 1
        self.trip_ref = ref
        self.trip_mid = mid
        self.trip_eff = eff
        self._clear_pending()

    def update(
        self, mid: Decimal, ts: Optional[float] = None, spread: Optional[Decimal] = None
    ) -> None:
        now = ts if ts is not None else time.monotonic()
        eff = self._effective_threshold(now)  # before recording this update's spread
        if spread is not None:
            self._spread_hist.append((now, spread))
        last_mid = self._hist[-1][1] if self._hist else mid
        step = mid - last_mid
        self._hist.append((now, mid))
        while self._hist and now - self._hist[0][0] > self.window:
            self._hist.popleft()
        window_ref = self._hist[0][1]
        window_move = mid - window_ref

        # Unambiguous shocks trip immediately: a single step >= 2x eff, a step
        # >= 2x the BASE threshold (spread scaling must never mute a true gap),
        # or a windowed cumulative move >= 2x eff (multi-step collapse).
        if abs(step) >= 2 * eff or abs(step) >= 2 * self.threshold:
            self._trip(now, last_mid, mid, eff)
            return
        if abs(window_move) >= 2 * eff:
            self._trip(now, window_ref, mid, eff)
            return

        # A stale candidate whose opening step aged out of the window expires.
        if self._pend_dir and now - self._pend_opened > self.window:
            self._clear_pending()

        # 1x-2x moves (single-step or windowed) open/extend a pending candidate
        # that must persist across confirm_updates same-direction updates.
        if abs(step) >= eff or abs(window_move) >= eff:
            move = step if abs(step) >= eff else window_move
            ref = last_mid if abs(step) >= eff else window_ref
            d = 1 if move > 0 else -1
            if d == self._pend_dir:
                self._pend_steps += 1
            else:
                self._pend_dir, self._pend_ref, self._pend_steps = d, ref, 1
                self._pend_opened = now
        elif self._pend_dir:
            sgn = (step > 0) - (step < 0)
            if sgn == self._pend_dir:
                self._pend_steps += 1  # grind continuing in the shock direction
            elif sgn == -self._pend_dir and abs(mid - self._pend_ref) < eff:
                self._clear_pending()  # reverted inside the band: false start
        if (
            self._pend_dir
            and self._pend_steps >= self.confirm_updates
            and self._pend_ref is not None
            and abs(mid - self._pend_ref) >= eff
        ):
            self._trip(now, self._pend_ref, mid, eff)

    def blocked(self, ts: Optional[float] = None) -> bool:
        now = ts if ts is not None else time.monotonic()
        return now < self._blocked_until


class QuotingGate:
    """Session-global quoting switch shared by every worker (2026-07-17, C1).

    The reconcile loop engages a cooloff when it detects an exchange-side
    sweep — every resting order vanished in one pass (order-group trip or
    maintenance pause with cancel_order_on_pause). Blindly re-arming into the
    market that just ran you over is how small losses become big ones, so all
    workers sit out until the cooloff expires. It re-arms automatically: this
    is a pause, NOT the kill switch (no HALTED marker). The gate also collects
    trading_is_paused rejections so a sweep event can attribute its likely
    cause (maintenance vs group trip).
    """

    def __init__(self) -> None:
        self._cooloff_until = 0.0
        self._pause_rejections: deque[float] = deque()

    def engage_cooloff(self, seconds: float, ts: Optional[float] = None) -> None:
        now = ts if ts is not None else time.monotonic()
        self._cooloff_until = max(self._cooloff_until, now + seconds)

    def blocked(self, ts: Optional[float] = None) -> bool:
        now = ts if ts is not None else time.monotonic()
        return now < self._cooloff_until

    def note_pause_rejection(self, ts: Optional[float] = None) -> None:
        self._pause_rejections.append(ts if ts is not None else time.monotonic())

    def pause_rejections_recent(self, window_s: float, ts: Optional[float] = None) -> int:
        now = ts if ts is not None else time.monotonic()
        cutoff = now - window_s
        while self._pause_rejections and self._pause_rejections[0] < cutoff:
            self._pause_rejections.popleft()
        return len(self._pause_rejections)


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
        reduce_only: bool = False,
        gate: Optional[QuotingGate] = None,
    ):
        self.ticker = ticker
        self.exchange = exchange
        self.strategy = strategy
        self.risk = risk
        self.events = events
        self.cfg = cfg
        self.dry_run = dry_run
        # reduce_only: quote only the side that shrinks the position (wind-down
        # worker for orphan positions). reduce_only_origin marks workers born
        # this way so the bench never "replaces" them.
        self.reduce_only = reduce_only
        self.reduce_only_origin = reduce_only
        self._wound_down = False
        # 2026-07-17 (C1): shared session gate (sweep cooloff). A private
        # default keeps single-worker tests and tools unchanged.
        self.gate = gate or QuotingGate()
        # 2026-07-17 (C1): set on a trading_is_paused rejection; the next
        # reconcile pass clears it and grants one fresh placement probe.
        # Round 2: _pause_suspected_at backs a 5-min self-clear fallback so a
        # wedged reconcile loop cannot strand markets suspended forever.
        self.pause_suspected = False
        self._pause_suspected_at = 0.0
        self.vol = VolEstimator(strategy.sigma_halflife_seconds, strategy.sigma_floor)
        self.guard = FastMoveGuard(
            cfg.fast_move_threshold,
            cfg.fast_move_window,
            cfg.fast_move_cooloff,
            cfg.fast_move_spread_multiple,
            cfg.fast_move_confirm_updates,
        )
        self._guard_announced = False
        self._guard_trips = 0
        # 2026-07-17 (H6): context of the trip whose cool-off is still open;
        # scored for persistence when the cool-off ends (see _resolve_guard_trip).
        # Round 2: latch the PRE-move reference — persistence means the mid
        # STAYED away from where it was before the move.
        self._guard_seen_trip = 0
        self._guard_trip_ref: Optional[Decimal] = None
        self._guard_trip_eff: Optional[Decimal] = None
        self.evicted = False
        # 2026-07-17 (M3): the close reaper flagged this market — quotes are
        # pulled and never re-placed (reduce_only wind-down overrides this: a
        # held position keeps exit quotes alive). Mids still flow to risk.
        self.close_reaped = False
        self.top: Optional[BookTop] = None
        self.bid_order: Optional[Order] = None
        self.ask_order: Optional[Order] = None
        self._dirty = asyncio.Event()
        self._last_requote = 0.0
        self._last_mid_mark = 0.0
        self._stopped = False
        # 2026-07-17 (M4): wind-down distress state (see _track_winddown).
        self._winddown_since: Optional[float] = None
        self._winddown_anchor_mid: Optional[Decimal] = None
        self._winddown_last_abs_q = 0
        self._winddown_distressed = False
        self._winddown_adverse = Decimal(0)
        self._last_distress_emit: Optional[float] = None
        # 2026-07-17 (M6): side -> (client_order_id, last-lookup monotonic,
        # consecutive empty lookups) for a create whose outcome is unknown
        # (timeout/5xx). Reconciled by client_order_id before that side
        # re-places; only declared lost after create_confirm_lost_lookups
        # consecutive empty lookups (2026-07-18, round 2).
        self._pending_create: dict[Side, tuple[str, float, int]] = {}

    # Called from the websocket consumer (same event loop).
    def on_book_top(self, top: BookTop) -> None:
        self.top = top
        mid = top.mid
        if mid is not None:
            self.vol.update(mid)
            spread = top.ask - top.bid  # mid is not None => both sides present
            self.guard.update(mid, spread=spread)
            self.risk.on_mid(self.ticker, mid)
            now = time.monotonic()
            if now - self._last_mid_mark >= self.cfg.mid_mark_interval:
                try:
                    self.events.record_mid(self.ticker, mid, top.bid, top.ask)
                except Exception:  # noqa: BLE001 — a DB hiccup must not starve the wake
                    log.exception("record_mid failed for %s", self.ticker)
                self._last_mid_mark = now
        self._dirty.set()

    def current_mid(self) -> Optional[Decimal]:
        return self.top.mid if self.top else None

    def wake(self) -> None:
        """Nudge the run loop to re-evaluate promptly — the reconcile pass
        uses this after clearing a vanished order ref or a pause suspension."""
        self._dirty.set()

    def _track_winddown(
        self, q: int, mid: Optional[Decimal], now: Optional[float] = None
    ) -> None:
        """Wind-down distress detector (2026-07-17, M4). A wind-down position
        is DISTRESSED when its exit has rested unfilled for
        winddown_alert_seconds (the market forgot us) or the mid has moved
        against the position by >= winddown_alert_move from the anchor (the
        market is fleeing — the 07-16 24c runner). Distress emits a loud
        winddown_distress event, throttled to one per
        winddown_distress_repeat_seconds per market. The anchor and clock
        re-arm on entry and on every partial fill (progress is not distress).
        `now` is injectable for tests."""
        now = time.monotonic() if now is None else now
        if not self.reduce_only or q == 0:
            self._winddown_since = None
            self._winddown_anchor_mid = None
            self._winddown_last_abs_q = 0
            self._winddown_distressed = False
            self._winddown_adverse = Decimal(0)
            return
        if self._winddown_since is None or abs(q) < self._winddown_last_abs_q:
            self._winddown_since = now
            self._winddown_anchor_mid = mid
            self._winddown_last_abs_q = abs(q)
            self._winddown_distressed = False
            self._winddown_adverse = Decimal(0)
            return
        elapsed = now - self._winddown_since
        adverse = Decimal(0)
        if mid is not None and self._winddown_anchor_mid is not None:
            delta = self._winddown_anchor_mid - mid
            adverse = delta if q > 0 else -delta
        self._winddown_adverse = max(adverse, Decimal(0))
        reasons = []
        if elapsed >= self.cfg.winddown_alert_seconds:
            reasons.append("stale_unfilled")
        if self._winddown_adverse >= self.cfg.winddown_alert_move:
            reasons.append("adverse_move")
        self._winddown_distressed = bool(reasons)
        if reasons and (
            self._last_distress_emit is None
            or now - self._last_distress_emit >= self.cfg.winddown_distress_repeat_seconds
        ):
            self._last_distress_emit = now
            self.events.emit(
                "winddown_distress",
                ticker=self.ticker,
                position=q,
                reason="+".join(reasons),
                elapsed_minutes=round(elapsed / 60, 1),
                anchor_mid=self._winddown_anchor_mid,
                mid=mid,
                adverse_move=self._winddown_adverse,
            )
            log.error(
                "WIND-DOWN DISTRESS %s: %+d unfilled %.1f min, adverse move %s (%s)",
                self.ticker, q, elapsed / 60, self._winddown_adverse, "+".join(reasons),
            )

    def _cross_worth_it(self, q: int, price: Decimal, size: int) -> bool:
        """Fee-model gate for the cross_1tick escalation (2026-07-17, M4):
        crossing converts a free maker exit into a fee-paying taker exit, so
        it is only worth doing when the taker fee for the whole exit is
        smaller than the adverse move already suffered on the position
        (paying cents to stop a bigger bleed)."""
        schedule = getattr(self.exchange, "fee_schedule", None)
        if schedule is None:
            return True  # no fee model configured: crossing costs nothing extra
        from .fees import compute_fee

        fee = compute_fee(schedule, size, price, is_taker=True)
        return fee <= self._winddown_adverse * abs(size)

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
        inventory = self.risk.markets.get(self.ticker)
        q = inventory.position if inventory else 0
        self._track_winddown(q, top.mid)

        if self.reduce_only and q == 0:
            # Wind-down complete: cancel anything resting and go inert.
            self.bid_order = await self._reconcile(Side.BID, self.bid_order, None, 0)
            self.ask_order = await self._reconcile(Side.ASK, self.ask_order, None, 0)
            if not self._wound_down:
                self._wound_down = True
                self.evicted = True  # drops us from the stream on next resubscribe
                self.events.emit("wind_down_complete", ticker=self.ticker)
                log.info("wind-down complete: %s is flat", self.ticker)
            return
        # 2026-07-17 (M3): close reaper pulled this market — cancel anything
        # resting and never re-quote (a reaped worker WITH a position was
        # converted to reduce_only instead and keeps exit quotes alive).
        if self.close_reaped and not self.reduce_only:
            self.bid_order = await self._reconcile(
                Side.BID, self.bid_order, None, 0, cancel_reason="close_reaper"
            )
            self.ask_order = await self._reconcile(
                Side.ASK, self.ask_order, None, 0, cancel_reason="close_reaper"
            )
            return
        if self.evicted and not self.reduce_only:
            return

        # 2026-07-17 (C1): global sweep cooloff — the reconcile pass saw every
        # resting order vanish exchange-side in one pass (order-group trip or
        # maintenance pause). Sit flat and re-arm on expiry. Unlike the
        # fast-move guard we do NOT keep exit quotes alive here: a sweep is a
        # market-structure event (the exchange itself is the counterparty
        # problem), not a fast move on a healthy exchange.
        if self.gate.blocked():
            self.bid_order = await self._reconcile(
                Side.BID, self.bid_order, None, 0, cancel_reason="sweep_cooloff"
            )
            self.ask_order = await self._reconcile(
                Side.ASK, self.ask_order, None, 0, cancel_reason="sweep_cooloff"
            )
            self._dirty.set()  # re-check when the cooloff ends
            return
        # 2026-07-17 (C1): this market rejected with trading_is_paused; stay
        # suspended until the next reconcile pass grants a fresh probe (or the
        # 5-minute fallback below, if the reconcile loop itself is wedged).
        if self.pause_suspected:
            if time.monotonic() - self._pause_suspected_at < 300:
                return
            self.pause_suspected = False

        # A new guard trip since the last cycle: latch its context. Counting it
        # toward eviction waits for the cool-off to end (H6 persistence check).
        if self.guard.trip_seq != self._guard_seen_trip:
            self._guard_seen_trip = self.guard.trip_seq
            self._guard_trip_ref = self.guard.trip_ref
            self._guard_trip_eff = self.guard.trip_eff

        blocked = self.guard.blocked()
        if blocked:
            if not self._guard_announced:
                self.events.emit(
                    "quotes_pulled", ticker=self.ticker, reason="fast_move",
                    mid=top.mid, cooloff=self.cfg.fast_move_cooloff,
                    confirmed_trips=self._guard_trips,
                )
                self._guard_announced = True
            if q == 0 and not self.reduce_only:
                self.bid_order = await self._reconcile(Side.BID, self.bid_order, None, 0)
                self.ask_order = await self._reconcile(Side.ASK, self.ask_order, None, 0)
                if not self.evicted:
                    self._dirty.set()  # resume when the cool-off ends
                return
            # Inventory during a cool-off: keep the EXIT side alive (verified
            # day-2 finding: freezing exits during a crash blocked the one
            # profitable escape), drop only the accumulating side below.
        else:
            self._guard_announced = False
            if self._guard_trip_ref is not None:
                self._resolve_guard_trip(top.mid)
                # Round 2 (adversarial): eviction decided this cycle must not
                # fall through to placement — the worker used to re-quote the
                # just-evicted market and leave the orders unmanaged to TTL.
                if self.evicted and not self.reduce_only:
                    self.bid_order = await self._reconcile(
                        Side.BID, self.bid_order, None, 0, cancel_reason="evicted"
                    )
                    self.ask_order = await self._reconcile(
                        Side.ASK, self.ask_order, None, 0, cancel_reason="evicted"
                    )
                    return

        mid = top.mid
        quotes = compute_quotes(
            mid=mid,
            inventory=q,
            max_inventory=self.risk.params.max_contracts_per_market,
            sigma=self.vol.sigma,
            p=self.strategy,
        )
        if not blocked:
            quotes = apply_join_best(
                quotes, top.bid, top.ask,
                min_book_spread=self.strategy.min_book_spread,
                join_margin=self.strategy.join_margin,
            )
        if self.reduce_only or blocked:
            # Exit-only quoting: suppress the side that would grow |position|,
            # cap the exit size at the position so we never flip through flat.
            if q > 0:
                quotes.bid, quotes.bid_size = None, 0
                quotes.ask_size = min(quotes.ask_size, q) if quotes.ask is not None else 0
            elif q < 0:
                quotes.ask, quotes.ask_size = None, 0
                quotes.bid_size = min(quotes.bid_size, -q) if quotes.bid is not None else 0
        # 2026-07-17 (M4): cross_1tick escalation. Runs ONLY when all of:
        # wind-down position + distressed (an alert already fired) + the owner
        # flipped winddown_escalation off its "none" default + the fee model
        # says crossing costs less than the bleed. Default config never enters
        # this block — the post-only invariant stays absolute.
        cross: dict[Side, Decimal] = {}
        if (
            self.reduce_only
            and q != 0
            and self._winddown_distressed
            and self.cfg.winddown_escalation == "cross_1tick"
        ):
            side = Side.ASK if q > 0 else Side.BID
            price = top.bid if q > 0 else top.ask  # hit the bid / lift the offer
            size = quotes.ask_size if q > 0 else quotes.bid_size
            if price is not None and size > 0 and self._cross_worth_it(q, price, size):
                cross[side] = price
                if q > 0:
                    quotes.ask = price
                else:
                    quotes.bid = price
                self.events.emit(
                    "winddown_escalated_cross",
                    ticker=self.ticker,
                    side=side.value,
                    price=price,
                    size=size,
                    position=q,
                    adverse_move=self._winddown_adverse,
                )
                log.error(
                    "WIND-DOWN ESCALATION %s: crossing as taker %s %d @ %.2f",
                    self.ticker, side.value, size, price,
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
            joined_bid=quotes.joined_bid,
            joined_ask=quotes.joined_ask,
            dry_run=self.dry_run,
        )
        self.bid_order = await self._reconcile(
            Side.BID, self.bid_order, quotes.bid, quotes.bid_size,
            post_only=Side.BID not in cross,
        )
        self.ask_order = await self._reconcile(
            Side.ASK, self.ask_order, quotes.ask, quotes.ask_size,
            post_only=Side.ASK not in cross,
        )

    def _resolve_guard_trip(self, mid_now: Decimal) -> None:
        """Score a finished cool-off (2026-07-17, H6): a trip counts toward
        eviction ONLY if the move persisted — at cool-off end the mid must
        still be >= eff/2 away from the PRE-move reference. A move that stuck
        is a real repricing (counts); one pulled level that bounced back is a
        false alarm (logged, not counted). Round 2: scoring vs the pre-move
        reference — the first version compared against the post-move mid,
        which inverted the classification."""
        trip_ref, eff = self._guard_trip_ref, self._guard_trip_eff
        self._guard_trip_ref = None
        confirmed = abs(mid_now - trip_ref) >= eff / 2
        if confirmed:
            self._guard_trips += 1
        self.events.emit(
            "guard_trip", ticker=self.ticker, confirmed=confirmed,
            trip_ref=trip_ref, mid_now=mid_now, eff_threshold=eff,
            trips=self._guard_trips,
        )
        if not confirmed:
            self.events.emit(
                "guard_false_alarm", ticker=self.ticker,
                trip_ref=trip_ref, mid_now=mid_now, eff_threshold=eff,
            )
            return
        if self._guard_trips >= self.cfg.guard_evict_trips and not self.evicted:
            st = self.risk.markets.get(self.ticker)
            q = st.position if st else 0
            self.evicted = True
            if q != 0:
                # Never abandon inventory: evicted-with-position becomes
                # a wind-down worker instead of going dark.
                self.reduce_only = True
            self.events.emit(
                "market_evicted", ticker=self.ticker,
                reason=f"{self._guard_trips} confirmed guard trips",
                trips=self._guard_trips, wind_down=self.reduce_only,
            )
            log.warning(
                "EVICTED %s: %d confirmed guard trips%s", self.ticker, self._guard_trips,
                " (wind-down mode: exit quotes only)" if self.reduce_only else "",
            )

    async def _reconcile(
        self, side: Side, existing: Optional[Order], price: Optional[Decimal], size: int,
        cancel_reason: Optional[str] = None,
        post_only: bool = True,
    ) -> Optional[Order]:
        if self.dry_run:
            return None
        # 2026-07-17 (M6): a create died ambiguously (timeout/5xx — the order
        # may be live exchange-side). Before placing anything new on this
        # side, wait out create_adopt_delay, then look up resting orders by
        # client_order_id: adopt the order if it landed (no double-place),
        # place fresh only when confirmed lost.
        pend = self._pending_create.get(side)
        if pend is not None:
            cid, last_at, empties = pend
            if time.monotonic() - last_at < self.cfg.create_adopt_delay:
                return existing  # ambiguity window: neither place nor churn
            del self._pending_create[side]
            adopted = await self._adopt_if_resting(side, cid)
            if adopted is not None:
                return adopted
            if side in self._pending_create:
                return existing  # lookup itself failed and re-parked; retry next cycle
            # 2026-07-18 (round 2): an empty lookup is not proof of loss — the
            # exchange's resting view is eventually consistent. Require several
            # consecutive empties (each after another adopt_delay) before
            # re-placing, so a just-landed order gets adopted rather than
            # double-placed.
            empties += 1
            if empties < self.cfg.create_confirm_lost_lookups:
                self._pending_create[side] = (cid, time.monotonic(), empties)
                return existing
            self.events.emit(
                "order_placement_confirmed_lost", ticker=self.ticker,
                side=side.value, client_order_id=cid, lookups=empties,
            )
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
                    # 2026-07-17: callers that pull for a specific reason (sweep
                    # cooloff) label it; the default keeps the old meanings.
                    reason=cancel_reason or ("guard_pull" if price is None else "requote"),
                )
            except Exception as e:  # noqa: BLE001
                self.events.emit(
                    "error", ticker=self.ticker, where="cancel", side=side.value, error=str(e)
                )
            # 2026-07-17 (C3): release the resting exposure whenever we drop the
            # reference — cancel, replacement, or TTL-expiry (the order is dead
            # exchange-side either way; if the cancel call itself failed, the
            # Pass-2 reconcile resyncs the registry).
            self.risk.release_order(self.ticker, existing.side, existing.count)
        if price is None or size <= 0:
            return None
        if self.gate.blocked():
            return None  # 2026-07-17 (C1): sweep cooloff engaged mid-cycle
        if self.pause_suspected:
            return None  # 2026-07-17 (C1): paused market; next probe after reconcile
        signed = size if side is Side.BID else -size
        ok, reason = self.risk.approve_order(self.ticker, signed, price)
        if not ok:
            self.events.emit(
                "order_blocked", ticker=self.ticker, side=side.value,
                price=price, size=size, reason=reason,
            )
            return None
        cid = new_client_order_id()
        try:
            order = await self.exchange.create_order(
                ticker=self.ticker,
                side=side,
                price=price,
                count=size,
                client_order_id=cid,
                expiration_seconds=self.cfg.order_ttl_seconds,
                post_only=post_only,
            )
            order.placed_monotonic = time.monotonic()
            # 2026-07-17 (C3): resting orders count toward the caps' worst case.
            self.risk.register_order(self.ticker, side, size)
            self.events.emit(
                "order_placed", ticker=self.ticker, side=side.value,
                order_id=order.order_id, price=price, size=size,
                post_only=post_only,
            )
            return order
        except Exception as e:  # noqa: BLE001
            if "paused" in str(e).lower():
                # 2026-07-17 (C1): trading_is_paused rejection — suspend THIS
                # market until the next reconcile pass re-arms it (one probe
                # per pass, not one rejection per second: 657 in 4 days). The
                # old exchange-global 300s backoff also froze healthy markets
                # when only one was paused; exchange-wide cases are now the
                # sweep detector's job (global cooloff via the gate).
                self.gate.note_pause_rejection()
                if not self.pause_suspected:
                    self.pause_suspected = True
                    self._pause_suspected_at = time.monotonic()
                    self.events.emit(
                        "quoting_suspended", ticker=self.ticker,
                        reason="trading_is_paused", until="next_reconcile_pass",
                    )
                    log.warning(
                        "%s: trading paused; suspending until next reconcile pass",
                        self.ticker,
                    )
                return None
            # 2026-07-17 (M6): a 4xx is a definitive rejection — the order is
            # NOT live. Anything else (timeout, connection reset, 5xx) is
            # ambiguous: it may have landed. Never blind-retry; park the
            # client_order_id and reconcile by it before this side re-places.
            status = getattr(e, "status", None)
            if status is not None and 400 <= status < 500:
                self.events.emit(
                    "order_rejected", ticker=self.ticker, side=side.value,
                    price=price, size=size, error=str(e),
                )
                return None
            self._pending_create[side] = (cid, time.monotonic(), 0)
            self.events.emit(
                "order_placement_unknown", ticker=self.ticker, side=side.value,
                client_order_id=cid, price=price, size=size, error=str(e),
            )
            log.warning(
                "%s %s create outcome unknown (%s); reconciling by client_order_id",
                self.ticker, side.value, e,
            )
            return None

    def parked_client_order_ids(self) -> set[str]:
        """client_order_ids of ambiguous creates awaiting adopt/confirm-lost
        (2026-07-18, round 2). The reconcile loop must NOT orphan-cancel these:
        the order may be live exchange-side and is about to be adopted."""
        return {cid for (cid, _at, _n) in self._pending_create.values()}

    async def _adopt_if_resting(self, side: Side, client_order_id: str) -> Optional[Order]:
        """Look up an ambiguously-placed order by client_order_id (2026-07-17,
        M6). If it is resting exchange-side, adopt it: register the C3
        exposure and track it like a normal placement."""
        try:
            resting = await self.exchange.get_resting_orders(self.ticker)
        except Exception as e:  # noqa: BLE001
            # Lookup failed: stay parked — better a missed cycle than a
            # double-place. A failed lookup is not an empty one, so it does not
            # count toward the confirm-lost tally (reset to 0). The next
            # _reconcile retries the lookup.
            self._pending_create[side] = (client_order_id, time.monotonic(), 0)
            self.events.emit(
                "error", ticker=self.ticker, where="adopt_lookup",
                side=side.value, error=str(e),
            )
            return None
        for o in resting:
            if o.client_order_id == client_order_id:
                o.placed_monotonic = time.monotonic()
                self.risk.register_order(self.ticker, side, o.count)
                self.events.emit(
                    "order_adopted", ticker=self.ticker, side=side.value,
                    order_id=o.order_id, client_order_id=client_order_id,
                    price=o.price, size=o.count,
                )
                log.warning(
                    "adopted ambiguous create as resting: %s %s %s",
                    self.ticker, side.value, o.order_id,
                )
                return o
        return None

    def order_filled(self, order_id: str, count: int) -> None:
        """Track a fill against our resting orders; forget fully-filled ones so the
        next reconcile re-places instead of cancelling a ghost."""
        for attr in ("bid_order", "ask_order"):
            order = getattr(self, attr)
            if order and order.order_id == order_id:
                order.count -= count
                # 2026-07-17 (C3): filled contracts move from resting exposure
                # into position — release them from the registry.
                self.risk.release_order(self.ticker, order.side, count)
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
                    self.events.emit(
                        "order_canceled", ticker=self.ticker, side=order.side.value,
                        order_id=order.order_id, price=order.price, reason="shutdown",
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("stop-cancel %s failed: %s", order.order_id, e)
                # 2026-07-17 (C3): session over, resting exposure is gone.
                self.risk.release_order(self.ticker, order.side, order.count)
        self.bid_order = self.ask_order = None
