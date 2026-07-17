"""Resting-order reconcile loop (2026-07-17, C1).

The bot subscribes only orderbook_delta + fill: exchange-initiated cancels are
invisible on the wire. Two real signatures motivated this:

  - nightly maintenance pauses cancel resting orders (every order carries
    cancel_order_on_pause): 657 trading_is_paused rejections in 4 days, and
    markets sat unquoted up to ~12 min (a worker only noticed at its 80% TTL
    refresh);
  - a Kalshi order-GROUP trip cancels EVERYTHING — the old bot noticed
    nothing and blindly re-armed ~12 min later into the market that had just
    run it over.

One global task (not per-market; reads are cheap — 8 markets / 45s is
trivial, and the loop stays sequential and small): fetch exchange-resting
orders, diff against each worker's local refs, then act:

  local ref missing exchange-side  -> `order_vanished`, release the C3 resting
                                      exposure, clear the ref so the worker
                                      re-quotes on its next evaluation;
  resting exchange-side, untracked -> `order_orphaned` + CANCEL it. A resting
                                      order we don't track is worse than none:
                                      it sits outside the caps, the kill
                                      switch's worldview, and our TTL refresh.
                                      flock already guarantees no second local
                                      instance could own it, and the loop runs
                                      only in live mode — observe never cancels.

If every quoted ticker (>= 2) loses orders in a single pass, that is the sweep
signature (group trip or maintenance pause): emit `exchange_sweep_detected`,
cancel-all as a cheap no-op safety net, and engage a global quoting cooloff
via the shared QuotingGate. The cooloff re-arms automatically on expiry —
this is not the kill switch; no HALTED marker is written.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .eventlog import EventLog
from .exchange.base import ExchangeAdapter, Order
from .marketmaker import MarketWorker, QuotingGate
from .risk import RiskManager

log = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    vanished: list[tuple[str, str, str]] = field(default_factory=list)  # (ticker, side, order_id)
    orphaned: list[str] = field(default_factory=list)  # canceled order ids
    sweep: bool = False


def _local_refs(workers: dict[str, MarketWorker]) -> dict[str, tuple[MarketWorker, str, Order]]:
    """order_id -> (worker, ref attribute name, order) for all tracked resting orders."""
    refs: dict[str, tuple[MarketWorker, str, Order]] = {}
    for w in workers.values():
        for attr in ("bid_order", "ask_order"):
            o = getattr(w, attr)
            if o is not None:
                refs[o.order_id] = (w, attr, o)
    return refs


async def reconcile_pass(
    exchange: ExchangeAdapter,
    workers: dict[str, MarketWorker],
    risk: RiskManager,
    events: EventLog,
    gate: QuotingGate,
    sweep_cooloff_seconds: float = 900.0,
) -> ReconcileReport:
    """One diff + repair cycle. Factored out of the loop for tests; the
    workers dict is read live so bench promotions mid-pass are fine."""
    report = ReconcileReport()
    resting = await exchange.get_resting_orders()
    on_exchange = {o.order_id: o for o in resting}
    refs = _local_refs(workers)
    vanished_candidates = {oid: ref for oid, ref in refs.items() if oid not in on_exchange}
    orphan_candidates = {oid: o for oid, o in on_exchange.items() if oid not in refs}

    if vanished_candidates or orphan_candidates:
        # Confirm against a fresh per-ticker fetch before touching anything:
        # the first fetch can straddle a worker's own cancel/replace (its ref
        # only updates after the new create returns), and we must never release
        # exposure a live replacement still needs, nor cancel an order a worker
        # placed while we were fetching.
        affected = sorted(
            {o.ticker for _, _, o in vanished_candidates.values()}
            | {o.ticker for o in orphan_candidates.values()}
        )
        fresh: dict[str, set[str]] = {}
        for ticker in affected:
            fresh[ticker] = {o.order_id for o in await exchange.get_resting_orders(ticker)}

        for oid, (w, attr, o) in vanished_candidates.items():
            if oid in fresh.get(o.ticker, set()):
                continue  # still resting — the first fetch straddled a replace
            if getattr(w, attr) is not o:
                continue  # the worker already replaced it while we fetched
            setattr(w, attr, None)
            risk.release_order(o.ticker, o.side, o.count)
            events.emit(
                "order_vanished", ticker=o.ticker, side=o.side.value,
                order_id=oid, price=o.price, count=o.count,
            )
            log.warning("order vanished exchange-side: %s %s %s", o.ticker, o.side.value, oid)
            w.wake()  # re-quote on the next evaluation
            report.vanished.append((o.ticker, o.side.value, oid))

        live_refs = _local_refs(workers)  # re-read: refs may have moved during the awaits
        for oid, o in orphan_candidates.items():
            if oid not in fresh.get(o.ticker, set()):
                continue  # left the book on its own (fill/TTL) — nothing to do
            if oid in live_refs:
                continue  # a worker claimed it while we were fetching
            try:
                await exchange.cancel_order(oid)
            except Exception as e:  # noqa: BLE001
                log.error("orphan cancel %s failed: %s", oid, e)
                events.emit(
                    "error", ticker=o.ticker, where="orphan_cancel",
                    order_id=oid, error=str(e),
                )
                continue
            events.emit(
                "order_orphaned", ticker=o.ticker, side=o.side.value,
                order_id=oid, price=o.price, count=o.count,
            )
            log.error("canceled orphaned resting order %s (%s %s)", oid, o.ticker, o.side.value)
            report.orphaned.append(oid)

    # Sweep signature: orders vanished across ALL quoted tickers (>= 2) in one
    # pass, none explained by local cancels — exactly the order-group-trip and
    # maintenance-pause-cancel patterns (both cancel literally everything; all
    # our orders carry cancel_order_on_pause). A single ticker's vanish — its
    # own pause, TTL expiry, or a fill racing the ws callback — is normal
    # churn, never a sweep.
    quoted = {o.ticker for _, _, o in refs.values()}
    vanished_tickers = {t for t, _, _ in report.vanished}
    if len(vanished_tickers) >= 2 and vanished_tickers == quoted:
        pauses = gate.pause_rejections_recent(900)
        suspected = "maintenance_pause" if pauses else "order_group_trip"
        events.emit(
            "exchange_sweep_detected",
            vanished_tickers=sorted(vanished_tickers),
            vanished_orders=len(report.vanished),
            pause_rejections_15m=pauses,
            suspected=suspected,
            cooloff_seconds=sweep_cooloff_seconds,
        )
        log.error(
            "EXCHANGE SWEEP (%s suspected): %d orders vanished across %s; cooloff %.0fs",
            suspected, len(report.vanished), sorted(vanished_tickers), sweep_cooloff_seconds,
        )
        gate.engage_cooloff(sweep_cooloff_seconds)
        try:
            await exchange.cancel_all_orders()  # cheap no-op safety net
        except Exception:  # noqa: BLE001
            log.exception("cancel-all after sweep failed — CHECK THE EXCHANGE UI")
        report.sweep = True

    # Re-arm pause suspensions: each suspended market gets one fresh placement
    # probe per pass; if it's still paused the rejection simply re-suspends it.
    # (Documented choice: the probe IS the exchange-state check — cheap,
    # self-limiting, and needs no extra market-status endpoint.)
    for w in list(workers.values()):
        if w.pause_suspected:
            w.pause_suspected = False
            w.wake()
            events.emit("quoting_resumed", ticker=w.ticker, reason="reconcile_pass")
    return report


async def reconcile_loop(
    exchange: ExchangeAdapter,
    workers: dict[str, MarketWorker],
    risk: RiskManager,
    events: EventLog,
    gate: QuotingGate,
    stop_event: asyncio.Event,
    reconcile_seconds: float,
    sweep_cooloff_seconds: float,
) -> None:
    """The global reconcile task. A failed pass (REST hiccup, DB error) logs
    and retries next cycle instead of killing the loop."""
    while not stop_event.is_set():
        await asyncio.sleep(reconcile_seconds)
        if stop_event.is_set():
            break
        try:
            await reconcile_pass(
                exchange, workers, risk, events, gate, sweep_cooloff_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("reconcile pass failed; retrying next cycle")
            try:
                events.emit("error", where="reconcile", error=str(e))
            except Exception:  # noqa: BLE001 — the DB may be the broken thing
                pass
