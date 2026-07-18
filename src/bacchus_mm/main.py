"""Orchestrator and CLI.

Commands:
  bacchus-mm markets            show what the selector would trade right now
  bacchus-mm observe            stream books for selected markets, log only, no orders
  bacchus-mm run                trade (demo env by default; prod needs live.enabled + --live)
  bacchus-mm cancel-all         cancel every resting order
  bacchus-mm halt-clear         acknowledge a kill-switch halt and remove the marker
  bacchus-mm analyze ...        log analysis reports (see bacchus_mm/analyze.py)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .config import Config
from .crossvenue import VenuePair, run_recorder
from .eventlog import EventLog
from .exchange.base import Fill
from .exchange.kalshi import KalshiAuth, KalshiExchange, rest_clock_skew_seconds
from .health import HealthState, start_health_server
from .marketmaker import MarketWorker, QuotingGate, WorkerConfig
from .reconcile import managed_tickers, reconcile_loop
from .risk import RiskManager, RiskParams
from .selector import select_markets

log = logging.getLogger("bacchus_mm")


def _load_env_file(root: Path) -> None:
    env_path = root / ".env"
    if not env_path.exists():
        return
    import os

    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _build_exchange(cfg: Config, need_auth: bool) -> KalshiExchange:
    creds = cfg.credentials()
    auth = None
    if creds.present:
        auth = KalshiAuth(creds.key_id, creds.private_key_pem())
    elif need_auth:
        sys.exit(
            "Missing credentials: set KALSHI_API_KEY_ID plus KALSHI_PRIVATE_KEY_PATH "
            "or KALSHI_PRIVATE_KEY (see .env.example). Demo keys come from "
            "demo.kalshi.co account settings."
        )
    return KalshiExchange(
        env=cfg.env, auth=auth, write_tokens_per_second=cfg.write_tokens_per_second,
        fee_schedule=cfg.fees.get("kalshi"),
        ws_recv_timeout_seconds=cfg.ws_recv_timeout_seconds,
    )


def supervise(
    task: asyncio.Task, name: str, stop_event: asyncio.Event, events: EventLog
) -> asyncio.Task:
    """Fail-stop task supervision (2026-07-17, H3). Every coroutine used to be
    fire-and-forget, so a dead task was a silent zombie — a dead risk_loop
    quietly disables the kill switch, a dead stream consumer leaves the bot
    quoting blind. On an unexpected exception: emit `task_died` and set
    stop_event so the normal shutdown path runs. Cancellation during shutdown
    (and any failure after stop_event is already set) is expected, not death.
    """

    def _done(t: asyncio.Task) -> None:
        if t.cancelled():
            return  # normal shutdown path
        exc = t.exception()
        if exc is None:
            return  # clean return (loops shouldn't, but benign)
        log.error("task %s died: %r", name, exc, exc_info=exc)
        if stop_event.is_set():
            return  # teardown races are expected once shutdown began
        try:
            events.emit(
                "task_died", task=name, error=repr(exc),
                traceback="".join(traceback.format_exception(exc)),
            )
        except Exception:  # noqa: BLE001 — the DB may be what killed it
            pass
        stop_event.set()

    task.add_done_callback(_done)
    return task


class FillDispatcher:
    """Fill fan-out with dedup and failure isolation (2026-07-17, H4 + H5).

    Dedup: Kalshi redelivers recent fills after a ws resubscribe; trade_id is
    the exchange's unique fill id, so a seen-set seeded from the fills table
    at startup drops redeliveries before they double-count position/PnL. A
    fill with a missing/empty trade id is processed normally — silently
    dropping a real fill would understate PnL, which is worse than the
    double-count the dedup exists to prevent.

    Ordering and isolation: risk.on_fill (the money state) applies first;
    record_fill is best-effort — a full/locked DB must neither desync the
    worker's order bookkeeping nor escape into kalshi.py's stream loop, where
    it used to be misreported as a ws disconnect; worker.order_filled always
    runs.
    """

    def __init__(self, workers: dict[str, MarketWorker], risk: RiskManager, events: EventLog):
        self.workers = workers
        self.risk = risk
        self.events = events
        self.seen: set[str] = events.known_trade_ids()

    def __call__(self, f: Fill) -> None:
        tid = f.trade_id or ""
        if tid and tid in self.seen:
            self.events.emit(
                "fill_duplicate_ignored", ticker=f.ticker, trade_id=tid,
                order_id=f.order_id, signed_count=f.signed_count,
            )
            log.warning("duplicate fill ignored: %s %s (%+d)", f.ticker, tid, f.signed_count)
            return
        w = self.workers.get(f.ticker)
        mid = w.current_mid() if w else None
        self.risk.on_fill(f.ticker, f.signed_count, f.yes_price, f.fee)
        if tid:
            # The fill is on the books now; ignore any redelivery even if the
            # record below fails.
            self.seen.add(tid)
        try:
            self.events.record_fill(
                f.ticker, f.trade_id, f.order_id, f.signed_count,
                f.yes_price, f.is_taker, mid, f.ts_ms,
                fee=f.fee, fee_source=f.fee_source,
            )
        except Exception:  # noqa: BLE001 — DB full/locked; stdlib log is the record
            log.exception("record_fill failed (trade %s) — fill applied, log row lost", tid)
            try:
                self.events.emit(
                    "fill_record_failed", ticker=f.ticker, trade_id=tid,
                    order_id=f.order_id, signed_count=f.signed_count,
                )
            except Exception:  # noqa: BLE001 — the DB is the broken thing
                pass
        if w:
            w.order_filled(f.order_id, abs(f.signed_count))
        fee_note = f" fee=${f.fee:.2f}({f.fee_source})" if f.fee else ""
        log.info(
            "FILL %s %+d @ %.2f (taker=%s)%s pos=%d pnl=$%.2f",
            f.ticker, f.signed_count, f.yes_price, f.is_taker, fee_note,
            self.risk.markets[f.ticker].position,
            self.risk.cumulative_pnl,  # 2026-07-17: cumulative, not session
        )


def load_chained_risk(params: RiskParams, state_dir: Path, events: EventLog) -> RiskManager:
    """RiskManager with the cross-session equity chain loaded from kv
    (2026-07-17, FIX-PnL). First run on a pre-upgrade DB: no kv rows -> offset
    0 and high_water anchors at 0 per the Pass-1 design (pre-upgrade losses
    intentionally not counted — see the comment in risk.py)."""
    offset = Decimal(events.kv_get("cumulative_pnl") or "0")
    hw = events.kv_get("high_water")
    return RiskManager(
        params=params,
        state_dir=state_dir,
        cumulative_offset=offset,
        high_water=Decimal(hw) if hw else None,
    )


def persist_pnl_marks(events: EventLog, risk: RiskManager) -> None:
    """Write the equity chain back to kv. str(Decimal) round-trips exactly —
    money math stays Decimal."""
    events.kv_set("cumulative_pnl", str(risk.cumulative_pnl))
    events.kv_set("high_water", str(risk.high_water))
    risk.new_high_water_since_load = False


def marks_tick(
    workers: dict[str, MarketWorker],
    risk: RiskManager,
    events: EventLog,
    tick_seconds: float,
    now: Optional[float] = None,
) -> int:
    """Periodic mark writer (2026-07-17, M3). Today mids only reach the mids
    table inside on_book_top — a market that goes quiet (frozen book, closed
    but unsettled, eviction) stops marking and its position pins at a stale
    mid. Every tick_seconds: write a mid mark for every market that hasn't
    marked itself recently, plus one pnl mark (risk_loop's 5s marks cover the
    kill switch; this guarantees a durable mark even in a dead-quiet
    session). Settled markets keep their final mark and are skipped."""
    now = time.monotonic() if now is None else now
    marked = 0
    for t, st in risk.markets.items():
        if st.last_mid is None or st.settled:
            continue
        w = workers.get(t)
        if w is not None and not w.evicted and now - w._last_mid_mark < tick_seconds:
            continue  # healthy worker already marking itself via book deltas
        bid = w.top.bid if w is not None and w.top is not None else None
        ask = w.top.ask if w is not None and w.top is not None else None
        events.record_mid(t, st.last_mid, bid, ask)
        marked += 1
    events.record_pnl(
        risk.cumulative_pnl, risk.high_water, risk.drawdown(), risk.gross_contracts()
    )
    return marked


def reap_closing_markets(
    workers: dict[str, MarketWorker],
    risk: RiskManager,
    events: EventLog,
    close_times: dict[str, str],
    reaper_hours: float,
    now: Optional[datetime] = None,
) -> list[str]:
    """Close reaper (2026-07-17, M3): min_hours_to_close is checked only at
    selection, so markets get quoted right into their close. When a market's
    hours-to-close drops below reaper_hours: pull its quotes and never
    re-quote it (the worker's close_reaped flag does the pulling on its next
    cycle; marking continues). A reaped market WITH a position converts to
    the existing wind-down machinery (reduce-only exit quotes until flat)
    rather than being abandoned. Emits close_reaper with hours-to-close."""
    now = datetime.now(timezone.utc) if now is None else now
    reaped: list[str] = []
    for t, close_iso in close_times.items():
        w = workers.get(t)
        if w is None or w.close_reaped or w.reduce_only or w.evicted:
            continue
        try:
            close_dt = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
        except ValueError:
            continue
        # 2026-07-18 (round 2): a naive close_time parses fine but subtracting
        # from tz-aware `now` raises TypeError (not ValueError), which would
        # abort the whole reaper tick at the first offender. Coerce to UTC.
        if close_dt.tzinfo is None:
            close_dt = close_dt.replace(tzinfo=timezone.utc)
        hours = (close_dt - now).total_seconds() / 3600
        if hours >= reaper_hours:
            continue
        st = risk.markets.get(t)
        q = st.position if st else 0
        w.close_reaped = True
        if q != 0:
            w.reduce_only = True  # wind-down machinery, same as evict-with-position
        w.wake()  # the worker pulls its own quotes on its next cycle
        events.emit(
            "close_reaper", ticker=t, hours_to_close=round(hours, 2),
            position=q, wind_down=q != 0,
        )
        log.warning(
            "close reaper: %s closes in %.1fh — quotes pulled%s",
            t, hours, "; position %+d -> wind-down" % q if q else "",
        )
        reaped.append(t)
    return reaped


async def settlement_poll(
    ex: KalshiExchange,
    risk: RiskManager,
    events: EventLog,
    close_times: dict[str, str],
    workers: Optional[dict[str, MarketWorker]] = None,
) -> list[str]:
    """Settlement realization (2026-07-17, M3): one pass over tickers with
    open positions. Kalshi's lifecycle is open -> closed -> determined ->
    (disputed -> amended)? -> finalized. Payout is $1 per YES on a yes result,
    $0 on no — realized into risk as position x (settlement - basis),
    net-of-fee (no settlement fee; fees were booked at fill time).

    2026-07-18 (round 2): realize ONLY at `finalized`. `determined` can still be
    disputed/amended, and realizing there locks in a possibly-wrong outcome that
    on_settlement freezes permanently (settled=True). The position stays visible
    and marked-to-mid (which tracks ~settlement post-determination anyway) until
    finalized. On realize we persist a settled marker (survives restart, so the
    still-reported position can't be double-realized) and retire the worker so a
    settled market stops quoting even if it determined before the close reaper's
    window. Emits settlement_realized and backfills close_times for the reaper."""
    realized: list[str] = []
    for t, st in list(risk.markets.items()):
        if st.position == 0 or st.settled:
            continue
        m = await ex.get_market_status(t)
        if m is None:
            continue
        if m.close_time:
            close_times.setdefault(t, m.close_time)
        if m.status == "finalized" and m.result in ("yes", "no"):
            settle = Decimal(1) if m.result == "yes" else Decimal(0)
            q, basis, pnl = risk.on_settlement(t, settle)
            events.mark_settled(t, str(settle))
            events.emit(
                "settlement_realized", ticker=t, position=q, basis=basis,
                settlement=settle, pnl=pnl, status=m.status,
            )
            log.info(
                "SETTLED %s: %+d @ basis %s -> %s, realized $%.2f",
                t, q, basis, m.result, pnl,
            )
            # Retire the worker: position is 0 now, so evicted drops it from
            # active_tickers() and stops re-quoting a dead market even if it
            # resolved before the reaper's 12h window.
            w = workers.get(t) if workers else None
            if w is not None:
                w.close_reaped = True
                w.evicted = True
                w.wake()
            realized.append(t)
    return realized


async def check_clock_skew(
    ex: KalshiExchange, events: EventLog, threshold_s: float = 2.0
) -> Optional[float]:
    """Startup clock-skew check (2026-07-17, DEPLOY): RSA-PSS auth signs with
    local-ms timestamps, so a skewed VPS clock fails auth opaquely. Advisory
    only — logs loudly and emits clock_skew_warning past the threshold but
    never blocks: Kalshi may tolerate small skew, and a failed check (DNS,
    proxy, no Date header) must not stop trading either."""
    try:
        skew = await rest_clock_skew_seconds(ex.rest_url)
    except Exception as e:  # noqa: BLE001 — advisory check, never fatal
        log.warning("clock-skew check failed (%s) — continuing", e)
        return None
    if abs(skew) > threshold_s:
        log.error(
            "CLOCK SKEW: local clock is %+.2fs vs Kalshi (|skew| > %.1fs) — "
            "RSA-PSS auth may fail; fix the host clock (NTP)",
            skew, threshold_s,
        )
        events.emit("clock_skew_warning", skew_s=round(skew, 3), threshold_s=threshold_s)
    else:
        log.info("clock skew vs Kalshi: %+.3fs (within %.1fs)", skew, threshold_s)
    return skew


def require_order_group(cfg: Config, live: bool, gid: Optional[str], events: EventLog) -> None:
    """Fail-closed order-group requirement (2026-07-17, M5). The Kalshi order
    group is the exchange-side runaway guard (REVIEW-2026-07-17 safety layer
    4): if the group fills more than N contracts in 15s, Kalshi cancels
    everything in it. Creation failure used to log a warning and trade on
    without it. On prod+live that is now fatal, unless the owner explicitly
    sets risk.allow_no_order_group."""
    if gid is not None:
        return
    if cfg.env == "prod" and live and not cfg.allow_no_order_group:
        events.emit(
            "startup_aborted", reason="order_group_unavailable", env=cfg.env,
        )
        sys.exit(
            "Refusing to trade on prod: the Kalshi order group (exchange-side "
            "runaway guard) could not be created — trading without safety "
            "layer 4 is fail-closed. Retry, or set risk.allow_no_order_group: "
            "true to explicitly accept the risk."
        )
    log.warning(
        "continuing WITHOUT an order group (%s)",
        "allow_no_order_group override" if cfg.allow_no_order_group else f"env={cfg.env}",
    )


async def cmd_markets(cfg: Config) -> None:
    ex = _build_exchange(cfg, need_auth=False)
    try:
        markets = await ex.list_markets()
        picks = select_markets(markets, cfg.selector)
        print(f"{len(markets)} open markets scanned; selector picked {len(picks)}:\n")
        for s in picks:
            m = s.market
            print(f"  {m.ticker:40s} score={s.score:.3f} [{m.category}] {', '.join(s.reasons)}")
            print(f"    {m.title}")
        if not picks:
            print("  (nothing eligible — loosen selector filters in config.local.yaml)")
    finally:
        await ex.close()


async def cmd_trade(cfg: Config, live: bool, dry_run: bool) -> None:
    if cfg.env == "prod":
        if dry_run:
            pass  # observing prod is always fine
        elif not (cfg.live_enabled and live):
            sys.exit(
                "Refusing to trade on prod: set live.enabled: true in config.local.yaml "
                "AND pass --live. (KALSHI_ENV=demo for the demo environment.)"
            )

    # Single-instance lock: two concurrent bots double exposure and fight over
    # each other's orders (observed: 44s dual-process overlap on 07-15). flock
    # releases automatically on any process death — no stale-lock handling needed.
    import fcntl

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    lock_handle = open(cfg.data_dir / "bot.lock", "w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Another bacchus-mm instance is already running (data/bot.lock is held).")

    ex = _build_exchange(cfg, need_auth=True)
    session_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    # 2026-07-17 (M1): batched eventlog — flush cadence/retention from config.
    events = EventLog(
        cfg.data_dir, session_id,
        flush_seconds=cfg.log_flush_seconds,
        flush_batch=cfg.log_flush_batch,
        events_keep_days=cfg.log_events_keep_days,
    )
    # 2026-07-17 (FIX-PnL): load the cross-session equity chain from kv.
    risk = load_chained_risk(cfg.risk, cfg.data_dir, events)

    # 2026-07-17 (DEPLOY): clock-skew check before any authed call — RSA-PSS
    # signs with local-ms timestamps (advisory: loud log + event, never blocks).
    await check_clock_skew(ex, events)

    prior_halt = risk.check_halt_file()
    if prior_halt and not dry_run:
        sys.exit(
            f"HALTED marker present from a previous kill-switch trip:\n  {prior_halt}\n"
            "Review data/ logs, then run `bacchus-mm halt-clear` to re-arm."
        )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    workers: dict[str, MarketWorker] = {}
    tasks: list[asyncio.Task] = []

    # 2026-07-17 (DEPLOY): liveness state for /health (fly.io machine checks).
    # Emitted events and the 5s risk_loop tick both heartbeat it; see health.py.
    health_state = HealthState(
        mode="observe" if dry_run else "run", live=live, risk=risk, workers=workers
    )
    events.on_event = health_state.note_event
    health_runner = None

    try:
        if cfg.health_enabled:
            try:
                health_runner = await start_health_server(health_state, cfg.health_port)
                log.info("health endpoint listening on :%d/health", cfg.health_port)
            except OSError:
                # Health is auxiliary — a bind failure must not take down
                # trading; the failed fly check surfaces it instead.
                log.exception("health endpoint failed to bind port %d", cfg.health_port)

        markets = await ex.list_markets()
        # Select extra markets as a standby bench: evicted workers get replaced
        # from it mid-session instead of the book shrinking all night.
        from dataclasses import replace as dc_replace

        all_picks = select_markets(
            markets, dc_replace(cfg.selector, max_markets=cfg.selector.max_markets + 8)
        )

        # Penalty box: tickers evicted in the last 48h are excluded — the same
        # flickery markets kept getting re-selected every restart (71% of all
        # guard trips came from two repeat offenders).
        import sqlite3 as _sq

        cutoff = int((time.time() - 48 * 3600) * 1000)
        _db = _sq.connect(f"file:{cfg.data_dir / 'bacchus.db'}?mode=ro", uri=True)
        boxed = {
            r[0]
            for r in _db.execute(
                "SELECT DISTINCT ticker FROM events WHERE type='market_evicted' AND ts_ms > ?",
                (cutoff,),
            )
        }
        _db.close()
        if boxed:
            log.info("penalty box (evicted <48h ago, excluded): %s", ", ".join(sorted(boxed)))
            all_picks = [s for s in all_picks if s.market.ticker not in boxed]

        # Falling-knife RANGE screen: the net-move filter misses round trips
        # (one market swung 66c in 24h and netted -10c). Check realized 24h
        # mid range from hourly candles for every candidate.
        screened = []
        for s_pick in all_picks:
            rng = await ex.get_24h_mid_range(s_pick.market.series_ticker, s_pick.market.ticker)
            if rng is not None and rng > cfg.selector.max_move_24h:
                events.emit(
                    "selection_rejected", ticker=s_pick.market.ticker,
                    reason="24h_range", range_24h=rng,
                )
                log.info("range screen: %s 24h swing %.2f > %.2f, skipped",
                         s_pick.market.ticker, rng, cfg.selector.max_move_24h)
                continue
            screened.append(s_pick)
        all_picks = screened

        picks = all_picks[: cfg.selector.max_markets]
        standby = [s.market for s in all_picks[cfg.selector.max_markets :]]
        if not picks:
            sys.exit("Selector found no eligible markets; try `bacchus-mm markets` and loosen filters.")
        tickers = [s.market.ticker for s in picks]

        balance = await ex.get_balance()
        positions = await ex.get_positions()

        def _last_logged_mid(ticker: str):
            # Round 2 (adversarial): seed held positions at the PRIOR session's
            # final mark, not the current mid — otherwise every repricing that
            # happens while the bot is down (nightly sleeps!) silently vanishes
            # from the cumulative chain and the kill switch never sees it.
            row = events.db.execute(
                "SELECT mid FROM mids WHERE ticker=? ORDER BY ts_ms DESC LIMIT 1", (ticker,)
            ).fetchone()
            return Decimal(str(row[0])) if row else None

        # 2026-07-18 (round 2): a position we already realized in a prior
        # session is still reported by the exchange until payout clears. Do NOT
        # re-seed it — its PnL is already baked into the persisted equity chain,
        # and re-seeding + a re-realize would double-count into the kill switch.
        already_settled = events.settled_tickers()
        if already_settled:
            log.info("skipping %d already-settled tickers at seed: %s",
                     len(already_settled), ", ".join(sorted(already_settled)))
        for s in picks:
            t = s.market.ticker
            if t in already_settled:
                continue
            if positions.get(t, 0):
                risk.seed_position(t, positions[t], _last_logged_mid(t) or s.market.mid)
            else:
                risk.seed_position(t, 0, s.market.mid)
        # Orphan positions (held in markets we no longer quote) stay marked so
        # PnL and the kill switch always see the whole book.
        for t, pos in positions.items():
            if t not in tickers and t not in already_settled:
                risk.seed_position(t, pos, _last_logged_mid(t))

        if not dry_run:
            gid = await ex.ensure_order_group(cfg.order_group_contracts_per_15s)
            # 2026-07-17 (M5): prod+live without the order group aborts here.
            require_order_group(cfg, live, gid, events)
            # 2026-07-17 (H2): scope the startup sweep to tickers we manage
            # (selected + held) — a second strategy on this account keeps its
            # orders. See managed_tickers for the flock caveat.
            stale = await ex.cancel_all_orders(
                tickers=managed_tickers(risk=risk, selected=tickers)
            )
            if stale:
                log.info("canceled %d stale resting orders from a previous session", stale)

        events.emit(
            "session_start",
            env=cfg.env,
            dry_run=dry_run,
            balance=balance,
            markets=tickers,
            positions=positions,
            config=cfg.raw,
        )
        log.info(
            "session %s: env=%s dry_run=%s balance=$%s markets=%s",
            session_id, cfg.env, dry_run, balance, ", ".join(tickers),
        )

        wcfg = WorkerConfig(
            requote_min_interval=cfg.requote_min_interval,
            requote_tolerance=cfg.requote_tolerance,
            order_ttl_seconds=cfg.order_ttl_seconds,
            fast_move_threshold=cfg.fast_move_threshold,
            fast_move_window=cfg.fast_move_window,
            fast_move_cooloff=cfg.fast_move_cooloff,
            fast_move_spread_multiple=cfg.fast_move_spread_multiple,
            fast_move_confirm_updates=cfg.fast_move_confirm_updates,
            guard_evict_trips=cfg.guard_evict_trips,
            # 2026-07-17 (M4): wind-down distress policy.
            winddown_alert_seconds=cfg.winddown_alert_minutes * 60,
            winddown_alert_move=cfg.winddown_alert_move,
            winddown_escalation=cfg.winddown_escalation,
        )
        # 2026-07-17 (M3): close times from selection (standby included);
        # the settlement poll backfills orphan tickers as it learns them.
        close_times = {s.market.ticker: s.market.close_time for s in all_picks}
        # 2026-07-17 (C1): one session-global quoting gate shared by every
        # worker, including wind-down workers and bench promotions.
        gate = QuotingGate()
        for t in tickers:
            workers[t] = MarketWorker(
                t, ex, cfg.strategy, risk, events, wcfg, dry_run=dry_run, gate=gate
            )
        # Wind-down workers: every orphan position gets exit-only quotes until
        # flat — the bot never leaves inventory unmanaged (owner directive
        # 2026-07-16, after an evicted market's short ran 24c with no exit).
        for t, pos in positions.items():
            if t not in workers and pos != 0:
                workers[t] = MarketWorker(
                    t, ex, cfg.strategy, risk, events, wcfg,
                    dry_run=dry_run, reduce_only=True, gate=gate,
                )
                events.emit("wind_down_started", ticker=t, position=pos)
                log.info("wind-down worker started for orphan position %s (%+d)", t, pos)

        _orphan_mark: dict[str, float] = {}

        def on_book_top(top):
            w = workers.get(top.ticker)
            if w:
                w.on_book_top(top)
            elif top.mid is not None:
                risk.on_mid(top.ticker, top.mid)
                if time.monotonic() - _orphan_mark.get(top.ticker, 0) >= 60:
                    events.record_mid(top.ticker, top.mid, top.bid, top.ask)
                    _orphan_mark[top.ticker] = time.monotonic()

        # 2026-07-17 (H4/H5): dedup + failure-isolated fill fan-out.
        on_fill = FillDispatcher(workers, risk, events)

        def active_tickers() -> list[str]:
            # Stream everything we quote, plus anything we still hold — evicted
            # or orphaned markets with open positions need mids for PnL marking.
            out = set()
            for t, w in workers.items():
                if not w.evicted:
                    out.add(t)
            for t, st in risk.markets.items():
                if st.position:
                    out.add(t)
            return sorted(out)

        async def consume_stream():
            async for _ in ex.stream(active_tickers, on_book_top, on_fill):
                pass

        async def bench_loop():
            """Replace evicted workers from the standby bench."""
            replaced: set[str] = set()
            while not stop_event.is_set():
                await asyncio.sleep(30)
                for t, w in list(workers.items()):
                    if not w.evicted or t in replaced or w.reduce_only_origin:
                        continue
                    replaced.add(t)
                    while standby:
                        sub = standby.pop(0)
                        if sub.ticker in workers:
                            continue
                        workers[sub.ticker] = MarketWorker(
                            sub.ticker, ex, cfg.strategy, risk, events, wcfg,
                            dry_run=dry_run, gate=gate,
                        )
                        risk.seed_position(sub.ticker, positions.get(sub.ticker, 0), sub.mid)
                        _spawn(workers[sub.ticker].run(), f"worker:{sub.ticker}")
                        events.emit(
                            "market_promoted", ticker=sub.ticker, replaces=t,
                            standby_remaining=len(standby),
                        )
                        log.info("promoted %s from standby (replacing evicted %s)", sub.ticker, t)
                        ex.request_resubscribe()
                        break
                    else:
                        log.warning("standby bench empty; %s not replaced", t)

        def _spawn(coro, name: str) -> None:
            # 2026-07-17 (H3): every long-lived task runs supervised (fail-stop).
            t = asyncio.create_task(coro, name=name)
            supervise(t, name, stop_event, events)
            tasks.append(t)

        async def risk_loop():
            last_kv_persist = time.monotonic()
            while not stop_event.is_set():
                await asyncio.sleep(5)
                # 2026-07-17 (DEPLOY): liveness heartbeat — a wedged loop stops
                # this and /health goes 503 (last_event_age_s > 300).
                health_state.note_event()
                # 2026-07-17 (FIX-PnL): mark the CUMULATIVE equity curve, not the
                # session-rebased one — rebasing understated true losses 2.9x
                # across the first 8 sessions. (Column names kept for compat;
                # session_high now carries the account high-water mark.)
                pnl = risk.cumulative_pnl
                dd = risk.drawdown()
                events.record_pnl(pnl, risk.high_water, dd, risk.gross_contracts())
                # 2026-07-17 (FIX-PnL): persist the equity chain — immediately
                # on a new account high-water (a crash must never lose the
                # kill-switch reference peak), otherwise at most once a minute
                # as cheap crash insurance for losses and stale values too.
                # Round 2: observe runs must never touch the equity chain — a
                # dry-run session has no fills but WOULD ratchet high_water on
                # transient marks of held positions.
                if not dry_run and (
                    risk.new_high_water_since_load or time.monotonic() - last_kv_persist >= 60
                ):
                    persist_pnl_marks(events, risk)
                    last_kv_persist = time.monotonic()
                reason = risk.should_halt()
                if reason and not risk.halted and not dry_run:
                    risk.halt(reason)
                    events.emit("halt", reason=reason, pnl=pnl, drawdown=dd)
                    log.error("KILL SWITCH: %s", reason)
                    try:
                        # 2026-07-17 (H2): cancel only what WE manage.
                        n = await ex.cancel_all_orders(
                            tickers=managed_tickers(workers, risk)
                        )
                        log.error("kill switch canceled %d resting orders; bot is halted", n)
                    except Exception:  # noqa: BLE001
                        log.exception("cancel-all during halt failed — CHECK THE EXCHANGE UI")
                    stop_event.set()

        async def eventlog_flush_loop():
            # 2026-07-17 (M1): time-based drain for the batched events/mids
            # queue (count-based flush happens inline on emit). Failures stay
            # non-fatal — the JSONL mirror is the durable firehose, and a sick
            # DB must not trip supervise() into halting the bot over logging.
            while not stop_event.is_set():
                await asyncio.sleep(cfg.log_flush_seconds)
                try:
                    events.flush()
                except Exception:  # noqa: BLE001
                    log.exception("eventlog flush failed")

        async def marks_loop():
            # 2026-07-17 (M3): periodic mid/pnl marks + the close reaper share
            # one slow tick. Failures stay non-fatal — a marking hiccup must
            # not halt trading (same rationale as eventlog_flush_loop).
            while not stop_event.is_set():
                await asyncio.sleep(cfg.marks_tick_seconds)
                try:
                    marks_tick(workers, risk, events, cfg.marks_tick_seconds)
                    reap_closing_markets(
                        workers, risk, events, close_times, cfg.close_reaper_hours
                    )
                except Exception:  # noqa: BLE001
                    log.exception("marks tick failed")

        async def settlement_poll_loop():
            # 2026-07-17 (M3): slow REST poll realizing settled positions.
            while not stop_event.is_set():
                await asyncio.sleep(cfg.settlement_poll_seconds)
                try:
                    await settlement_poll(ex, risk, events, close_times, workers)
                except Exception:  # noqa: BLE001
                    log.exception("settlement poll failed")

        _spawn(consume_stream(), "stream")
        _spawn(risk_loop(), "risk_loop")
        _spawn(bench_loop(), "bench_loop")
        _spawn(eventlog_flush_loop(), "eventlog_flush")
        _spawn(marks_loop(), "marks_loop")  # 2026-07-17 (M3)
        # 2026-07-18 (round 2): settlement realization MUTATES risk cash/PnL, so
        # it is live-only — the same gate reconcile uses. In observe it would
        # book phantom money into a dry-run session's PnL. marks_loop stays
        # unconditional (pure logging + reaper quote-pulls, no cash mutation).
        if not dry_run:
            _spawn(settlement_poll_loop(), "settlement_poll")  # 2026-07-17 (M3)
        for t in list(workers):
            _spawn(workers[t].run(), f"worker:{t}")
        # 2026-07-17 (C1): one global resting-order reconcile task — see
        # reconcile.py. Live mode only: observe must never cancel anything,
        # and orphan-cancel assumes this process is the account's only writer
        # (flock guarantees that locally).
        if not dry_run:
            _spawn(
                reconcile_loop(
                    ex, workers, risk, events, gate, stop_event,
                    cfg.reconcile_seconds, cfg.sweep_cooloff_seconds,
                    ttl_seconds=cfg.order_ttl_seconds,
                ),
                "reconcile",
            )

        # Cross-venue recorder rides along whenever pairs are configured — one
        # command ingests everything. `bacchus-mm crossvenue` still runs it alone.
        xv = cfg.raw.get("crossvenue", {}) or {}
        xv_pairs = [VenuePair.from_config(p) for p in xv.get("pairs", [])]
        if xv_pairs:
            log.info("cross-venue recorder attached: %d pairs", len(xv_pairs))
            _spawn(
                run_recorder(xv_pairs, ex, events, float(xv.get("poll_seconds", 15))),
                "crossvenue",
            )

        await stop_event.wait()
        log.info("shutting down…")
    finally:
        for w in workers.values():
            try:
                await w.stop()
            except Exception:  # noqa: BLE001
                log.exception("worker stop failed")
        for t in tasks:
            t.cancel()
        if not dry_run:
            try:
                # 2026-07-17 (H2): shutdown cancel scoped to our tickers —
                # worker.stop() already canceled tracked refs; this catches
                # stragglers on tickers we manage, not the whole account.
                managed = managed_tickers(workers, risk)
                remaining = await ex.cancel_all_orders(tickers=managed)
                # 2026-07-18 (round 2): the "still resting" check is scoped to
                # OUR tickers too — an account-wide read counts a second
                # strategy's legitimate orders (the whole point of H2) and
                # false-fires this alarm while masking a genuine straggler.
                managed_set = set(managed)
                resting = [
                    o for o in await ex.get_resting_orders() if o.ticker in managed_set
                ]
                events.emit("session_stop", canceled=remaining, still_resting=len(resting))
                if resting:
                    log.error(
                        "%d orders STILL RESTING on managed tickers after shutdown "
                        "— check the exchange UI", len(resting)
                    )
                else:
                    log.info("shutdown clean: no resting orders on managed tickers")
            except Exception:  # noqa: BLE001
                log.exception("shutdown cancel-all failed — CHECK THE EXCHANGE UI")
        else:
            events.emit("session_stop", canceled=0, still_resting=0)
        # 2026-07-17 (FIX-PnL): leave the equity chain durable on every exit —
        # halt, signal, or plain shutdown. (Round 2: live sessions only.)
        if not dry_run:
            try:
                persist_pnl_marks(events, risk)
            except Exception:  # noqa: BLE001
                log.exception("failed to persist cumulative pnl on shutdown")
        # 2026-07-17 (M1): close() drains the batched queue first — every
        # shutdown path (normal, halt, exception) funnels through here.
        events.close()
        if health_runner is not None:
            try:
                await health_runner.cleanup()
            except Exception:  # noqa: BLE001 — teardown must not hang
                log.exception("health server cleanup failed")
        await ex.close()


async def cmd_crossvenue(cfg: Config) -> None:
    raw = cfg.raw.get("crossvenue", {}) or {}
    pairs = [VenuePair.from_config(p) for p in raw.get("pairs", [])]
    if not pairs:
        sys.exit(
            "No pairs configured. Add a crossvenue: section to config.local.yaml —\n"
            "see src/bacchus_mm/crossvenue.py for the format, and use\n"
            "`bacchus-mm pm-find \"cpi\"` to look up Polymarket slugs."
        )
    ex = _build_exchange(cfg, need_auth=False)
    session_id = f"xv-{time.strftime('%Y%m%d-%H%M%S')}"
    events = EventLog(
        cfg.data_dir, session_id,
        flush_seconds=cfg.log_flush_seconds,
        flush_batch=cfg.log_flush_batch,
        events_keep_days=cfg.log_events_keep_days,
    )
    log.info("cross-venue recorder: %d pairs, poll every %ss", len(pairs), raw.get("poll_seconds", 15))
    try:
        await run_recorder(pairs, ex, events, float(raw.get("poll_seconds", 15)))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        events.close()
        await ex.close()


async def cmd_pm_find(query: str) -> None:
    from .exchange.polymarket import PolymarketData

    pm = PolymarketData()
    try:
        matches = await pm.find_markets(query)
        if not matches:
            print(f"no active Polymarket markets matching {query!r} in the top-volume set")
            return
        for m in matches[:20]:
            print(f"{m.slug}")
            print(f"  {m.question}")
            print(f"  outcomes={m.outcomes} vol24h=${m.volume_24h:,.0f} ends={m.end_date}")
    finally:
        await pm.close()


async def cmd_selftest(cfg: Config, live: bool) -> None:
    """Order-plumbing proof: place a 1-contract post-only bid at $0.01 on the
    most liquid selected market, confirm it rests, cancel it, confirm it's gone.
    Worst case cost if somehow filled: one cent. Gated like `run`."""
    from decimal import Decimal

    from .exchange.base import Side
    from .exchange.kalshi import new_client_order_id

    if cfg.env == "prod" and not (cfg.live_enabled and live):
        sys.exit(
            "selftest places a real (1-contract, $0.01) order: set live.enabled: true "
            "in config.local.yaml AND pass --live."
        )
    ex = _build_exchange(cfg, need_auth=True)
    events = EventLog(
        cfg.data_dir, f"selftest-{time.strftime('%Y%m%d-%H%M%S')}",
        flush_seconds=cfg.log_flush_seconds,
        flush_batch=cfg.log_flush_batch,
        events_keep_days=cfg.log_events_keep_days,
    )
    try:
        markets = await ex.list_markets()
        picks = select_markets(markets, cfg.selector)
        if not picks:
            sys.exit("selector found no markets to test against")
        ticker = picks[0].market.ticker
        print(f"placing 1 @ $0.01 post-only bid on {ticker} …")
        order = await ex.create_order(
            ticker=ticker,
            side=Side.BID,
            price=Decimal("0.01"),
            count=1,
            client_order_id=new_client_order_id(),
            expiration_seconds=120,
        )
        events.emit("selftest_order_placed", ticker=ticker, order_id=order.order_id)
        print(f"  placed: {order.order_id} (status {order.status})")

        await asyncio.sleep(2)
        resting = {o.order_id for o in await ex.get_resting_orders()}
        if order.order_id not in resting:
            events.emit("selftest_failed", ticker=ticker, reason="order not resting")
            sys.exit("FAIL: order not found resting after placement — investigate before go-live")
        print("  confirmed resting on the book")

        await ex.cancel_order(order.order_id)
        await asyncio.sleep(2)
        resting = {o.order_id for o in await ex.get_resting_orders()}
        if order.order_id in resting:
            events.emit("selftest_failed", ticker=ticker, reason="order still resting after cancel")
            sys.exit("FAIL: cancel did not remove the order — CHECK THE EXCHANGE UI")
        events.emit("selftest_passed", ticker=ticker, order_id=order.order_id)
        print("  canceled and confirmed gone.")
        print("PASS: create -> rest -> cancel round trip verified. Order plumbing is live-ready.")
    finally:
        events.close()
        await ex.close()


async def cmd_equity(cfg: Config) -> None:
    """True mark-to-market across sessions: free cash + position values from the
    exchange, marked at the latest logged mids. Survives restarts, unlike the
    session-rebased pnl_marks."""
    import sqlite3
    from decimal import Decimal

    ex = _build_exchange(cfg, need_auth=True)
    try:
        balance = await ex.get_balance()
        positions = await ex.get_positions()
        db = sqlite3.connect(cfg.data_dir / "bacchus.db")
        equity = balance
        print(f"free cash:              ${balance:.2f}")
        for ticker, pos in sorted(positions.items()):
            row = db.execute(
                "SELECT mid FROM mids WHERE ticker=? ORDER BY ts_ms DESC LIMIT 1", (ticker,)
            ).fetchone()
            mid = Decimal(str(row[0])) if row else Decimal("0.5")
            mark = "" if row else " (no logged mid; marked at 0.50)"
            value = pos * mid if pos > 0 else abs(pos) * (1 - mid)
            equity += value
            print(f"  {ticker:36s} {pos:+4d} @ mid {mid}  -> ${value:.2f}{mark}")
        print(f"equity:                 ${equity:.2f}")
    finally:
        await ex.close()


async def cmd_cancel_all(cfg: Config) -> None:
    ex = _build_exchange(cfg, need_auth=True)
    try:
        n = await ex.cancel_all_orders()
        print(f"canceled {n} resting orders")
        resting = await ex.get_resting_orders()
        print(f"{len(resting)} orders still resting")
    finally:
        await ex.close()


def cmd_halt_clear(cfg: Config) -> None:
    """Acknowledge a kill-switch halt. Clearing also REBASES the persisted
    high-water mark to current cumulative PnL — "halt-clear" means "I accept
    this loss level; protect me from here." Without the rebase, cumulative
    drawdown is still >= the threshold at the next start and the bot re-halts
    immediately with no recovery path (the 2026-07-17 halt-loop trap)."""
    risk = RiskManager(params=cfg.risk, state_dir=cfg.data_dir)
    reason = risk.check_halt_file()
    if reason is None:
        print("no HALTED marker present")
        return
    risk.clear_halt()
    events = EventLog(cfg.data_dir, f"halt-clear-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        cum = events.kv_get("cumulative_pnl") or "0"
        old_hw = events.kv_get("high_water") or "0"
        events.kv_set("high_water", cum)
        events.emit(
            "halt_cleared", reason=reason,
            old_high_water=old_hw, rebased_high_water=cum,
        )
    finally:
        events.close()
    print(f"cleared halt: {reason}")
    print(
        f"kill switch re-armed: high-water rebased ${old_hw} -> ${cum}; "
        f"drawdown now measures from the acknowledged level"
    )


def cli() -> None:
    parser = argparse.ArgumentParser(prog="bacchus-mm", description="Kalshi market-making bot")
    parser.add_argument("--root", default=".", help="project root (config + data dir)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("markets", help="show current selector picks")
    sub.add_parser("observe", help="stream + log selected markets, place no orders")
    run_p = sub.add_parser("run", help="trade")
    run_p.add_argument("--live", action="store_true", help="required (with live.enabled) for prod")
    sub.add_parser("cancel-all", help="cancel all resting orders")
    sub.add_parser("equity", help="true mark-to-market: cash + positions at latest mids")
    sub.add_parser("halt-clear", help="acknowledge a kill-switch halt")
    st = sub.add_parser("selftest", help="1-cent order round-trip plumbing test (gated like run)")
    st.add_argument("--live", action="store_true")
    sub.add_parser("crossvenue", help="record kalshi vs polymarket divergence for mapped pairs")
    pf = sub.add_parser("pm-find", help="search active Polymarket markets to build pair mappings")
    pf.add_argument("query")
    an = sub.add_parser("analyze", help="log analysis reports")
    an.add_argument("report", nargs="?", default="summary",
                    choices=["summary", "markouts", "quotes", "incidents", "divergence"])
    an.add_argument("--hours", type=float, default=24.0)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    root = Path(args.root)
    _load_env_file(root)
    cfg = Config.load(root)

    if args.command == "markets":
        asyncio.run(cmd_markets(cfg))
    elif args.command == "observe":
        asyncio.run(cmd_trade(cfg, live=False, dry_run=True))
    elif args.command == "run":
        asyncio.run(cmd_trade(cfg, live=args.live, dry_run=False))
    elif args.command == "crossvenue":
        asyncio.run(cmd_crossvenue(cfg))
    elif args.command == "pm-find":
        asyncio.run(cmd_pm_find(args.query))
    elif args.command == "cancel-all":
        asyncio.run(cmd_cancel_all(cfg))
    elif args.command == "equity":
        asyncio.run(cmd_equity(cfg))
    elif args.command == "halt-clear":
        cmd_halt_clear(cfg)
    elif args.command == "selftest":
        asyncio.run(cmd_selftest(cfg, live=args.live))
    elif args.command == "analyze":
        from .analyze import run_report

        run_report(cfg.data_dir, args.report, args.hours)


if __name__ == "__main__":
    cli()
