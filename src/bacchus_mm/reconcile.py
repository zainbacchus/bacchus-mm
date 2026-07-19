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


def managed_tickers(
    workers: dict[str, MarketWorker] | None = None,
    risk: RiskManager | None = None,
    selected: list[str] | tuple[str, ...] = (),
) -> list[str]:
    """Every ticker WE manage (2026-07-17, H2): all worker tickers (including
    evicted ones — their orders may still rest until TTL) plus anything we
    hold a position in, plus an optional explicit selection. Account-wide
    cancels (startup sweep, kill switch, shutdown, sweep safety net) scope to
    this set so a second strategy on the same account is left alone. The
    single-writer flock assumption still stands for UNKNOWN orders: an order
    we don't track on one of our tickers is still orphan-canceled by the
    reconcile pass. Empty set cancels NOTHING (fail-safe direction)."""
    out = set(selected)
    for t in (workers or {}):
        out.add(t)
    for t, st in (risk.markets if risk else {}).items():
        if st.position:
            out.add(t)
    return sorted(out)


@dataclass
class ReconcileReport:
    vanished: list[tuple[str, str, str]] = field(default_factory=list)  # (ticker, side, order_id)
    orphaned: list[str] = field(default_factory=list)  # canceled order ids
    ttl_explained: int = 0  # vanished orders old enough that TTL expiry explains them
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
    ttl_seconds: float = 900.0,
    clock_jumped: bool = False,
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

        import time as _time

        for oid, (w, attr, o) in vanished_candidates.items():
            if oid in fresh.get(o.ticker, set()):
                continue  # still resting — the first fetch straddled a replace
            if getattr(w, attr) is not o:
                continue  # the worker already replaced it while we fetched
            setattr(w, attr, None)
            risk.release_order(o.ticker, o.side, o.count)
            age = _time.monotonic() - o.placed_monotonic if o.placed_monotonic else None
            events.emit(
                "order_vanished", ticker=o.ticker, side=o.side.value,
                order_id=oid, price=o.price, count=o.count, age_seconds=age,
            )
            log.warning("order vanished exchange-side: %s %s %s", o.ticker, o.side.value, oid)
            w.wake()  # re-quote on the next evaluation
            report.vanished.append((o.ticker, o.side.value, oid))
            if age is not None and age >= 0.8 * ttl_seconds:
                report.ttl_explained += 1

        live_refs = _local_refs(workers)  # re-read: refs may have moved during the awaits
        # 2026-07-18 (round 2): client_order_ids of ambiguous M6 creates awaiting
        # adopt/confirm-lost. These orders may be live exchange-side and are
        # about to be adopted by their worker — cancelling them here would
        # defeat M6's no-double-place guarantee and leave the side unquoted.
        parked_cids: set[str] = set()
        for w in workers.values():
            parked_cids |= w.parked_client_order_ids()
        for oid, o in orphan_candidates.items():
            if oid not in fresh.get(o.ticker, set()):
                continue  # left the book on its own (fill/TTL) — nothing to do
            if oid in live_refs:
                continue  # a worker claimed it while we were fetching
            if (o.client_order_id or "") in parked_cids:
                continue  # a worker's ambiguous create is reconciling this one
            if not (o.client_order_id or "").startswith("bmm-"):
                # Round 2 (adversarial): flock only guards local processes — an
                # order the owner placed by hand in the Kalshi UI is NOT ours to
                # cancel. Log it and leave it alone.
                events.emit(
                    "order_foreign", ticker=o.ticker, side=o.side.value,
                    order_id=oid, price=o.price, count=o.count,
                )
                log.warning(
                    "foreign resting order on %s (%s) — not bot-tagged, leaving it",
                    o.ticker, oid,
                )
                continue
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
            # 2026-07-18 (round 2): wake the owning-ticker worker so it re-quotes
            # the now-empty side promptly instead of waiting for its 60s fallback
            # tick (the orphan path used to only wake on the vanished path).
            w = workers.get(o.ticker)
            if w is not None:
                w.wake()

    # Sweep signature: orders vanished across ALL quoted tickers (>= 2) in one
    # pass, none explained by local cancels — exactly the order-group-trip and
    # maintenance-pause-cancel patterns (both cancel literally everything; all
    # our orders carry cancel_order_on_pause). A single ticker's vanish — its
    # own pause, TTL expiry, or a fill racing the ws callback — is normal
    # churn, never a sweep.
    # Round 2 (adversarial): the strict vanished==quoted set equality both
    # false-fired (synchronized TTL-refresh bursts; post-sleep wakes where every
    # order TTL-expired) and was defeated by a single surviving/replaced order.
    # New trigger: a mass vanish by COUNT (>= max(4, half the refs), across >= 2
    # tickers), with three vetoes — TTL-age (old orders expiring is not a
    # sweep), clock jump (first pass after a sleep gap classifies nothing), and
    # a 2s settle-and-confirm refetch: after a genuine sweep the exchange
    # rejects re-placements (group tripped / trading paused), so if ANY of our
    # tagged orders is back on the book, it was ordinary churn — stand down.
    vanished_tickers = {t for t, _, _ in report.vanished}
    swept_count = len(report.vanished) - report.ttl_explained
    if (
        not clock_jumped
        and len(vanished_tickers) >= 2
        and swept_count >= max(4, (len(refs) + 1) // 2)
    ):
        await asyncio.sleep(2)
        try:
            settled = await exchange.get_resting_orders()
        except Exception:  # noqa: BLE001 — can't confirm -> don't engage
            settled = None
        ours_back = (
            None
            if settled is None
            else [o for o in settled if (o.client_order_id or "").startswith("bmm-")]
        )
        if ours_back is not None and not ours_back:
            pauses = gate.pause_rejections_recent(900)
            suspected = "maintenance_pause" if pauses else "order_group_trip"
            events.emit(
                "exchange_sweep_detected",
                vanished_tickers=sorted(vanished_tickers),
                vanished_orders=len(report.vanished),
                ttl_explained=report.ttl_explained,
                pause_rejections_15m=pauses,
                suspected=suspected,
                cooloff_seconds=sweep_cooloff_seconds,
            )
            log.error(
                "EXCHANGE SWEEP (%s suspected): %d orders vanished across %s; cooloff %.0fs",
                suspected, len(report.vanished), sorted(vanished_tickers),
                sweep_cooloff_seconds,
            )
            gate.engage_cooloff(sweep_cooloff_seconds)
            try:
                # 2026-07-17 (H2): scope the safety net to tickers we manage —
                # account-wide cancels block sharing the account with a second
                # strategy. Everything the sweep just vanished is in this set.
                await exchange.cancel_all_orders(tickers=managed_tickers(workers, risk))
            except Exception:  # noqa: BLE001
                log.exception("cancel-all after sweep failed — CHECK THE EXCHANGE UI")
            report.sweep = True

    # Position-drift adoption (Round 2): fills missed while the ws was down
    # (or a dropped in-flight message) leave local position wrong until
    # restart. Compare exchange truth and adopt it, cash-adjusted at the
    # current mark so the correction itself is PnL-neutral at adoption time.
    try:
        ex_positions = await exchange.get_positions()
    except Exception:  # noqa: BLE001 — next pass retries
        ex_positions = None
    if ex_positions is not None:
        from decimal import Decimal as _D

        for t in set(ex_positions) | set(risk.markets):
            ex_pos = ex_positions.get(t, 0)
            st = risk.markets.get(t)
            local = st.position if st else 0
            if ex_pos == local:
                continue
            events.emit("position_drift", ticker=t, local=local, exchange=ex_pos)
            log.warning("position drift on %s: local %+d exchange %+d — adopting", t, local, ex_pos)
            if st is None:
                risk.seed_position(t, ex_pos, None)
            else:
                mark = st.last_mid if st.last_mid is not None else _D("0.5")
                st.cash -= (ex_pos - local) * mark
                st.position = ex_pos

    # Re-arm pause suspensions: each suspended market gets one fresh placement
    # probe per pass; if it's still paused the rejection simply re-suspends it.
    # (Documented choice: the probe IS the exchange-state check — cheap,
    # self-limiting, and needs no extra market-status endpoint.)
    for w in list(workers.values()):
        if w.pause_suspected:
            w.pause_suspected = False
            w.wake()
            events.emit("quoting_resumed", ticker=w.ticker, reason="probe_granted")
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
    ttl_seconds: float = 900.0,
) -> None:
    """The global reconcile task. A failed pass (REST hiccup, DB error) logs
    and retries next cycle instead of killing the loop; a hung pass is timed
    out so the pause re-arm path can never be starved (Round 2)."""
    import time as _time

    last_pass = _time.monotonic()
    while not stop_event.is_set():
        await asyncio.sleep(reconcile_seconds)
        if stop_event.is_set():
            break
        now = _time.monotonic()
        # First pass after a sleep gap: everything expired while we were dark;
        # classify nothing as a sweep this pass.
        clock_jumped = now - last_pass > 3 * reconcile_seconds
        last_pass = now
        try:
            await asyncio.wait_for(
                reconcile_pass(
                    exchange, workers, risk, events, gate, sweep_cooloff_seconds,
                    ttl_seconds=ttl_seconds, clock_jumped=clock_jumped,
                ),
                timeout=max(30.0, reconcile_seconds),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("reconcile pass failed; retrying next cycle")
            try:
                events.emit("error", where="reconcile", error=str(e))
            except Exception:  # noqa: BLE001 — the DB may be the broken thing
                pass
