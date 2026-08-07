"""Phase D orchestrator: measurement quoting on the 15-minute markets.

`bacchus-mm fifteen --live` runs join-the-touch quoting (1 lot per side by
default) on every series in fifteen.series, rolling to each new 15-minute
window as it opens. There is NO selector, NO Avellaneda-Stoikov pricing, and
NO fast-move guard here — the point is to measure what passive queue
membership at the touch actually earns (fill rate, conditional edge vs
settlement), which research/ARB-AND-15MIN-STUDY-2026-08-06.md could not
simulate. Deliberate consequences of that goal:

  * The guard is disabled (threshold parked at $9). These books reprice
    constantly; a guard would change the measured policy. Risk is bounded by
    quote_size x max_contracts_per_market x $1 per series instead.
  * Quotes are pulled at close - pull_seconds_before_close (default 75s): the
    last 60s IS the settlement averaging window — a resting quote there gives
    a free option on a number that is being fixed while price still moves.
  * Fills ride to settlement (defined risk <= $1/contract); the settlement
    poll realizes them. No wind-down quoting for window positions.

Legacy positions from the calm-MM era still get reduce-only wind-down workers
(A-S path, join_touch_only=False), so one deployment both winds down the old
book and runs the measurement.

Window discovery polls /events per series and refuses any market whose price
structure it cannot parse (fifteen_structure_refused) — quoting an unknown
tick grid with real money is how you buy something you did not price.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field, replace as dc_replace
from datetime import datetime
from decimal import Decimal
from typing import Optional

from .config import Config
from .eventlog import EventLog
from .exchange.kalshi import KalshiExchange
from .fees import series_of
from .health import HealthState, start_health_server
from .marketmaker import MarketWorker, QuotingGate, WorkerConfig
from .reconcile import managed_tickers, reconcile_loop

log = logging.getLogger(__name__)

DEFAULT_SERIES = [
    "KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M",
    "KXHYPE15M", "KXNEAR15M", "KXGOLD15M", "KXSILVER15M",
]

# 2026-08-06 (M4): Coinbase spot products per series, for the defensive
# jump-pull feed. BNB/HYPE trade nowhere on Coinbase and Gold/Silver have no
# free tick feed — those series simply run without M4 (config-overridable).
DEFAULT_SPOT_PRODUCTS = {
    "KXBTC15M": "BTC-USD",
    "KXETH15M": "ETH-USD",
    "KXSOL15M": "SOL-USD",
    "KXDOGE15M": "DOGE-USD",
    "KXNEAR15M": "NEAR-USD",
}


def detect_jump(samples: list, window_s: float, now: float) -> float:
    """Max absolute move (in basis points) between the LATEST price and any
    sample inside the trailing window. samples: [(ts, price), ...] appended in
    time order. Pure function for testability."""
    if len(samples) < 2:
        return 0.0
    t_last, p_last = samples[-1]
    worst = 0.0
    for t, p in reversed(samples[:-1]):
        if now - t > window_s:
            break
        if p > 0:
            worst = max(worst, abs(p_last / p - 1.0) * 10_000)
    return worst


@dataclass
class FifteenParams:
    series: list[str] = field(default_factory=lambda: list(DEFAULT_SERIES))
    quote_size: int = 1
    max_contracts_per_market: int = 5
    # Cancel everything this many seconds before close; the final 60s is the
    # settlement averaging window (see module docstring).
    pull_seconds_before_close: float = 75.0
    # Don't join a window in its first seconds — the book is still forming.
    min_seconds_after_open: float = 5.0
    discovery_poll_seconds: float = 10.0
    requote_min_interval: float = 1.0
    # Two decicent ticks of slack before chasing the touch — every requote is
    # a cancel+create (12 write tokens) and forfeits queue position.
    requote_tolerance: Decimal = Decimal("0.002")
    order_ttl_seconds: int = 60
    settlement_poll_seconds: float = 300.0
    # Accept a window only if its price grid is a single uniform step inside
    # this band. Anything else is a structure we did not study.
    min_tick: Decimal = Decimal("0.0001")
    max_tick: Decimal = Decimal("0.01")
    # A "15 minute" market whose open->close span is outside this band is not
    # the product we studied (e.g. a mislabeled daily).
    min_window_seconds: float = 60.0
    max_window_seconds: float = 3600.0
    # ---- 2026-08-06 evidence levers (each independently disable-able) ----
    # M1 favorite-longshot tilt (join_touch.py). Calibrated on 1,440 settled
    # 15M windows 2026-08-06: late-window favorites at 0.98-1.00 settle
    # +0.57c/ct above price (n=2005, ~4 sigma); the longshot mirror is
    # negative. 0 disables.
    tilt_tail_threshold: Decimal = Decimal("0.90")
    # M3 price-shaped inventory cap in worst-case dollars per market
    # (BS-for-PM handbook): long caps at max_loss/p, short at max_loss/(1-p),
    # never above max_contracts_per_market. At p=0.50 this equals the flat
    # cap 5. 0 disables.
    max_loss_per_market: Decimal = Decimal("2.50")
    # M2 toxicity pull: the fast-move guard re-enabled with 15M-scale
    # parameters (Bartlett 2026: one-sided flow predicts maker losses).
    # Threshold 9 disables (the measurement-mode default before this).
    fast_move_threshold: Decimal = Decimal("0.03")
    fast_move_window: float = 10.0
    fast_move_cooloff: float = 45.0
    # M4 spot-jump defensive pull (Budish et al: don't be the stale quote).
    # A move of jump_bps within jump_window_seconds on the series' spot feed
    # cancels that series' quotes for jump_cooloff_seconds. 0 bps disables.
    spot_jump_bps: float = 8.0
    spot_jump_window_seconds: float = 10.0
    spot_jump_cooloff_seconds: float = 20.0
    spot_poll_seconds: float = 2.0
    spot_products: dict = field(default_factory=lambda: dict(DEFAULT_SPOT_PRODUCTS))

    @classmethod
    def from_config(cls, raw: dict) -> "FifteenParams":
        f = raw.get("fifteen", {}) or {}
        d = cls()
        return cls(
            series=list(f.get("series", d.series)),
            quote_size=int(f.get("quote_size", d.quote_size)),
            max_contracts_per_market=int(
                f.get("max_contracts_per_market", d.max_contracts_per_market)
            ),
            pull_seconds_before_close=float(
                f.get("pull_seconds_before_close", d.pull_seconds_before_close)
            ),
            min_seconds_after_open=float(
                f.get("min_seconds_after_open", d.min_seconds_after_open)
            ),
            discovery_poll_seconds=float(
                f.get("discovery_poll_seconds", d.discovery_poll_seconds)
            ),
            requote_min_interval=float(
                f.get("requote_min_interval", d.requote_min_interval)
            ),
            requote_tolerance=Decimal(str(f.get("requote_tolerance", d.requote_tolerance))),
            order_ttl_seconds=int(f.get("order_ttl_seconds", d.order_ttl_seconds)),
            settlement_poll_seconds=float(
                f.get("settlement_poll_seconds", d.settlement_poll_seconds)
            ),
            min_tick=Decimal(str(f.get("min_tick", d.min_tick))),
            max_tick=Decimal(str(f.get("max_tick", d.max_tick))),
            min_window_seconds=float(f.get("min_window_seconds", d.min_window_seconds)),
            max_window_seconds=float(f.get("max_window_seconds", d.max_window_seconds)),
            tilt_tail_threshold=Decimal(str(f.get("tilt_tail_threshold", d.tilt_tail_threshold))),
            max_loss_per_market=Decimal(str(f.get("max_loss_per_market", d.max_loss_per_market))),
            fast_move_threshold=Decimal(str(f.get("fast_move_threshold", d.fast_move_threshold))),
            fast_move_window=float(f.get("fast_move_window", d.fast_move_window)),
            fast_move_cooloff=float(f.get("fast_move_cooloff", d.fast_move_cooloff)),
            spot_jump_bps=float(f.get("spot_jump_bps", d.spot_jump_bps)),
            spot_jump_window_seconds=float(
                f.get("spot_jump_window_seconds", d.spot_jump_window_seconds)
            ),
            spot_jump_cooloff_seconds=float(
                f.get("spot_jump_cooloff_seconds", d.spot_jump_cooloff_seconds)
            ),
            spot_poll_seconds=float(f.get("spot_poll_seconds", d.spot_poll_seconds)),
            spot_products=dict(f.get("spot_products", d.spot_products)),
        )


def start_stall_watchdog(
    loop_thread_id: int,
    beat: dict,
    threshold: float = 1.5,
    interval: float = 0.25,
    report_every: float = 10.0,
):
    """2026-08-07 stall investigation, stage 2. The loop_lag probe PROVED the
    event loop freezes for seconds at a time (it can only report afterwards);
    this watchdog runs on a separate THREAD, watches the probe's heartbeat,
    and the moment the loop goes silent it samples the main thread's stack
    via sys._current_frames() — an in-process py-spy. The offending frame of
    the next stall lands in the log verbatim. Log-only from the thread (the
    EventLog is not thread-safe); logging itself is queue-based/non-blocking.
    """
    import threading
    import traceback

    def watch():
        last_report = 0.0
        while True:
            time.sleep(interval)
            silent = time.monotonic() - beat["t"]
            if silent > threshold and time.monotonic() - last_report > report_every:
                last_report = time.monotonic()
                frame = sys._current_frames().get(loop_thread_id)
                if frame is not None:
                    stack = "".join(traceback.format_stack(frame))
                    log.warning(
                        "STALL WATCHDOG: loop silent %.1fs; main-thread stack:\n%s",
                        silent, stack,
                    )

    t = threading.Thread(target=watch, daemon=True, name="stall-watchdog")
    t.start()
    return t


async def _idle_until_halt_cleared(risk, stop: asyncio.Event, poll_seconds: float = 15.0) -> bool:
    """Wait until the operator removes the HALTED marker (halt-clear) or a
    stop signal arrives. True = marker gone (restart to trade); False =
    signaled while still halted."""
    while not stop.is_set():
        if not risk.check_halt_file():
            return True
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            pass
    return not risk.check_halt_file()


def _ts(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_window(m: dict, p: FifteenParams, now: Optional[float] = None):
    """Validate one raw market payload as a quotable 15-minute window.

    Returns (ticker, open_ts, close_ts, finest_tick) or (None, reason) — the
    caller refuses to quote anything this function cannot fully parse.

    Price grids: verified live 2026-08-06, the crypto 15M series use a
    PIECEWISE grid — 0.001 steps in the tails ([0,0.10] and [0.90,1.00]) and
    0.01 in the middle — while Gold/Silver are uniform 0.01. We accept any
    sorted, contiguous, gap-free cover of [0,1] whose every step is inside
    [min_tick, max_tick]. The join-touch policy is grid-safe by construction
    (it only quotes at prices ALREADY resting in the book), so the tick is
    returned for logging/strategy metadata, not price generation.
    """
    now = now if now is not None else time.time()
    ticker = m.get("ticker")
    if not ticker:
        return None, "no_ticker"
    open_ts, close_ts = _ts(m.get("open_time")), _ts(m.get("close_time"))
    if open_ts is None or close_ts is None:
        return None, "unparseable_times"
    span = close_ts - open_ts
    if not (p.min_window_seconds <= span <= p.max_window_seconds):
        return None, f"window_span_{int(span)}s"
    parsed_ranges: list[tuple[Decimal, Decimal, Decimal]] = []
    for r in m.get("price_ranges") or []:
        try:
            parsed_ranges.append(
                (Decimal(str(r.get("start"))), Decimal(str(r.get("end"))),
                 Decimal(str(r.get("step"))))
            )
        except (TypeError, ValueError, ArithmeticError):
            return None, "unparseable_price_ranges"
    if not parsed_ranges:
        return None, "no_price_ranges"
    parsed_ranges.sort(key=lambda r: r[0])
    if parsed_ranges[0][0] != 0 or parsed_ranges[-1][1] != 1:
        return None, "ranges_do_not_cover_0_1"
    prev_end = parsed_ranges[0][0]
    for start, end, step in parsed_ranges:
        if start != prev_end or end <= start:
            return None, "ranges_gap_or_overlap"
        if not (p.min_tick <= step <= p.max_tick):
            return None, f"tick_out_of_band_{step}"
        prev_end = end
    finest = min(step for _, _, step in parsed_ranges)
    return (ticker, open_ts, close_ts, finest), None


async def run_fifteen(cfg: Config, live: bool, dry_run: bool) -> None:
    # Late import: main.py imports this module lazily from cli(), and we reuse
    # its session plumbing — module-level cross-imports would be circular.
    from .main import (
        FillDispatcher,
        check_clock_skew,
        load_chained_risk,
        marks_tick,
        persist_pnl_marks,
        require_order_group,
        settlement_poll,
        supervise,
        _build_exchange,
    )

    p = FifteenParams.from_config(cfg.raw)

    if cfg.env == "prod":
        if dry_run:
            pass
        elif not (cfg.live_enabled and live):
            sys.exit(
                "Refusing to trade on prod: set live.enabled: true (or "
                "BACCHUS_LIVE_ENABLED=1) AND pass --live."
            )
        elif not cfg.loaded_files and os.environ.get("BACCHUS_ALLOW_NO_CONFIG") != "1":
            sys.exit(
                "Refusing to trade on prod: NO config file was loaded (the 8-day "
                "silent mis-config). Ship config.yaml or set BACCHUS_ALLOW_NO_CONFIG=1."
            )

    import fcntl

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    lock_handle = open(cfg.data_dir / "bot.lock", "w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Another bacchus-mm instance is already running (data/bot.lock is held).")

    ex: KalshiExchange = _build_exchange(cfg, need_auth=True)
    session_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    events = EventLog(
        cfg.data_dir, session_id,
        flush_seconds=cfg.log_flush_seconds,
        flush_batch=cfg.log_flush_batch,
        events_keep_days=cfg.log_events_keep_days,
    )
    # Fifteen inventory cap replaces the calm-mode cap; everything else in
    # risk (notional caps, kill switch) comes from config unchanged.
    risk_params = dc_replace(cfg.risk, max_contracts_per_market=p.max_contracts_per_market)
    risk = load_chained_risk(risk_params, cfg.data_dir, events)

    await check_clock_skew(ex, events)

    prior_halt = risk.check_halt_file()
    if prior_halt and not dry_run:
        # 2026-08-06 incident: on fly, sys.exit here crash-looped the machine
        # every ~15 min (restart policy "always") and the ~5s uptime window
        # made `fly ssh console -C "bacchus-mm halt-clear"` impossible. IDLE
        # instead: keep the process (and ssh) alive, poll for the operator
        # removing the marker via halt-clear, then exit 0 for a clean fresh
        # boot. The halt itself still requires the deliberate human action —
        # nothing auto-clears.
        log.error(
            "HALTED marker present from a previous kill-switch trip:\n  %s\n"
            "Idling (no orders will be placed). Run "
            "`bacchus-mm --root /app halt-clear` (fly: via ssh console) to re-arm; "
            "this process exits for a clean restart once the marker is gone.",
            prior_halt,
        )
        events.emit("halted_idle", reason=prior_halt)
        idle_stop = asyncio.Event()
        idle_loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            idle_loop.add_signal_handler(sig, idle_stop.set)
        cleared = await _idle_until_halt_cleared(risk, idle_stop)
        events.emit("halted_idle_end", cleared=cleared)
        events.close()
        await ex.close()
        if cleared:
            log.error("halt marker cleared; exiting 0 for a clean restart")
            sys.exit(0)
        return  # stopped by signal while still halted

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    workers: dict[str, MarketWorker] = {}
    window_close: dict[str, float] = {}  # ticker -> close epoch (window workers only)
    close_times: dict[str, str] = {}  # ISO strings for the settlement poll
    tasks: list[asyncio.Task] = []

    health_state = HealthState(
        mode="fifteen" if not dry_run else "fifteen-observe",
        live=live, risk=risk, workers=workers,
    )
    events.on_event = health_state.note_event
    health_runner = None

    try:
        if cfg.health_enabled:
            try:
                health_runner = await start_health_server(health_state, cfg.health_port)
                log.info("health endpoint listening on :%d/health", cfg.health_port)
            except OSError:
                log.exception("health endpoint failed to bind port %d", cfg.health_port)

        balance = await ex.get_balance()
        positions = await ex.get_positions()

        def _last_logged_mid(ticker: str):
            row = events.db.execute(
                "SELECT mid FROM mids WHERE ticker=? ORDER BY ts_ms DESC LIMIT 1", (ticker,)
            ).fetchone()
            return Decimal(str(row[0])) if row else None

        # Seed every held position (all "orphans" here — fifteen selects no
        # standing markets); skip already-realized ones exactly like cmd_trade.
        already_settled = events.settled_tickers()
        for t, pos in positions.items():
            if pos and t not in already_settled:
                risk.seed_position(t, pos, _last_logged_mid(t))

        if not dry_run:
            gid = await ex.ensure_order_group(cfg.order_group_contracts_per_15s)
            require_order_group(cfg, live, gid, events)
            stale = await ex.cancel_all_orders(tickers=managed_tickers(risk=risk, selected=[]))
            if stale:
                log.info("canceled %d stale resting orders from a previous session", stale)

        events.emit(
            "session_start",
            env=cfg.env,
            dry_run=dry_run,
            mode="fifteen",
            balance=balance,
            markets=[],
            positions=positions,
            config=cfg.raw,
            config_files=cfg.loaded_files,
            effective_params={
                "series": p.series,
                "quote_size": p.quote_size,
                "max_contracts_per_market": p.max_contracts_per_market,
                "pull_seconds_before_close": p.pull_seconds_before_close,
                "requote_tolerance": p.requote_tolerance,
                "order_ttl_seconds": p.order_ttl_seconds,
                "kill_switch_drawdown": risk_params.kill_switch_drawdown,
            },
        )
        log.info(
            "fifteen session %s: env=%s dry_run=%s balance=$%s series=%s",
            session_id, cfg.env, dry_run, balance, ",".join(p.series),
        )

        # Window workers: join-touch policy, guard disabled (measurement mode
        # — see module docstring), dense mid marks for 15-minute lifetimes.
        wcfg_window = WorkerConfig(
            requote_min_interval=p.requote_min_interval,
            requote_tolerance=p.requote_tolerance,
            order_ttl_seconds=p.order_ttl_seconds,
            # 15s (was 5s): mids are also written by marks_tick and at every
            # quote change; 5s tripled the row rate for no analytical gain and
            # fed the event-loop starvation seen in the first live hour.
            mid_mark_interval=15.0,
            # M2 (2026-08-06): the guard IS the toxicity pull, re-enabled with
            # 15M-scale parameters (was parked at $9 in measurement mode).
            # Eviction stays effectively off: a window lives 15 minutes, so
            # pull-and-resume is the only sane response to a trip.
            fast_move_threshold=p.fast_move_threshold,
            fast_move_window=p.fast_move_window,
            fast_move_cooloff=p.fast_move_cooloff,
            guard_evict_trips=10_000,
            join_touch_only=True,
            # M1 + M3 (2026-08-06): see join_touch.py for sources/semantics.
            join_tilt_threshold=(p.tilt_tail_threshold or None),
            join_max_loss_per_market=(p.max_loss_per_market or None),
            # audit 2026-08-06: dedup identical decisions (30s heartbeat) —
            # these books wake workers ~1/s; unthrottled that is ~0.5GB/day of
            # identical rows and a full fly volume mid-week.
            quote_decision_min_interval=30.0,
        )
        # Legacy wind-down workers: standard calm-mode config (A-S exit path).
        wcfg_legacy = WorkerConfig(
            requote_min_interval=cfg.requote_min_interval,
            requote_tolerance=cfg.requote_tolerance,
            order_ttl_seconds=cfg.order_ttl_seconds,
            fast_move_threshold=cfg.fast_move_threshold,
            fast_move_window=cfg.fast_move_window,
            fast_move_cooloff=cfg.fast_move_cooloff,
            fast_move_spread_multiple=cfg.fast_move_spread_multiple,
            fast_move_confirm_updates=cfg.fast_move_confirm_updates,
            guard_evict_trips=cfg.guard_evict_trips,
            winddown_alert_seconds=cfg.winddown_alert_minutes * 60,
            winddown_alert_move=cfg.winddown_alert_move,
            winddown_escalation=cfg.winddown_escalation,
        )
        strategy_window = dc_replace(
            cfg.strategy, quote_size=p.quote_size, tick=Decimal("0.001")
        )
        gate = QuotingGate()

        fifteen_series = set(p.series)
        for t, pos in positions.items():
            if pos == 0 or t in already_settled:
                continue
            # 2026-08-06 (audit): a held FIFTEEN-series position (mid-window
            # restart) must NOT get a wind-down worker — the A-S exit path is
            # not tracked by pull_loop, so its quotes would rest straight into
            # the settlement averaging window. Window positions ride to
            # settlement by design; the poll realizes them. Defined risk
            # <= max_contracts_per_market x $1.
            if series_of(t) in fifteen_series:
                events.emit("fifteen_position_rides", ticker=t, position=pos)
                log.info("held fifteen position rides to settlement: %s (%+d)", t, pos)
                continue
            workers[t] = MarketWorker(
                t, ex, cfg.strategy, risk, events, wcfg_legacy,
                dry_run=dry_run, reduce_only=True, gate=gate,
            )
            events.emit("wind_down_started", ticker=t, position=pos)
            log.info("wind-down worker started for legacy position %s (%+d)", t, pos)

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

        on_fill = FillDispatcher(workers, risk, events)

        def active_tickers() -> list[str]:
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

        def _spawn(coro, name: str) -> None:
            t = asyncio.create_task(coro, name=name)
            supervise(t, name, stop_event, events)
            tasks.append(t)

        refused: set[str] = set()

        async def discovery_loop():
            """Poll each series for its open window; spawn a worker per window."""
            while not stop_event.is_set():
                new_windows = 0
                for s in p.series:
                    try:
                        mkts = await ex.get_series_open_markets(s)
                    except Exception as e:  # noqa: BLE001 — one series must not stall the rest
                        log.warning("discovery failed for %s: %s", s, e)
                        continue
                    now = time.time()
                    for m in mkts:
                        parsed, reason = parse_window(m, p, now)
                        if parsed is None:
                            tk = m.get("ticker") or f"{s}-?"
                            if tk not in refused:
                                refused.add(tk)
                                events.emit(
                                    "fifteen_structure_refused",
                                    ticker=tk, series=s, reason=reason,
                                )
                                log.warning("refusing window %s: %s", tk, reason)
                            continue
                        tkr, open_ts, close_ts, tick = parsed
                        if tkr in workers or tkr in already_settled:
                            continue
                        if now < open_ts + p.min_seconds_after_open:
                            continue  # book still forming; next poll gets it
                        if now >= close_ts - p.pull_seconds_before_close:
                            continue  # too late in the window to join
                        w = MarketWorker(
                            tkr, ex, dc_replace(strategy_window, tick=tick),
                            risk, events, wcfg_window, dry_run=dry_run, gate=gate,
                        )
                        workers[tkr] = w
                        window_close[tkr] = close_ts
                        close_times[tkr] = m.get("close_time", "")
                        risk.seed_position(tkr, positions.get(tkr, 0), None)
                        _spawn(w.run(), f"window:{tkr}")
                        events.emit(
                            "fifteen_window_start",
                            ticker=tkr, series=s, tick=tick,
                            open_time=m.get("open_time"), close_time=m.get("close_time"),
                            seconds_to_close=round(close_ts - now, 1),
                        )
                        log.info(
                            "window %s: quoting joins for %.0fs (tick %s)",
                            tkr, close_ts - now - p.pull_seconds_before_close, tick,
                        )
                        new_windows += 1
                if new_windows:
                    ex.request_resubscribe()
                await asyncio.sleep(p.discovery_poll_seconds)

        # Windows past close, awaiting flat + a grace period before removal —
        # 9 series x ~96 windows/day would otherwise grow `workers` (and its
        # idle run() tasks) without bound.
        awaiting_cleanup: dict[str, float] = {}

        async def pull_loop():
            """Second-granularity close handling: pull quotes at T-pull, retire
            the worker at close, remove it once flat. Positions ride to
            settlement by design."""
            while not stop_event.is_set():
                now = time.time()
                for tkr, close_ts in list(window_close.items()):
                    w = workers.get(tkr)
                    if w is None:
                        window_close.pop(tkr, None)
                        continue
                    if now >= close_ts - p.pull_seconds_before_close and not w.close_reaped:
                        w.close_reaped = True
                        w.wake()
                        st = risk.markets.get(tkr)
                        events.emit(
                            "fifteen_quotes_pulled",
                            ticker=tkr,
                            seconds_to_close=round(close_ts - now, 1),
                            position=st.position if st else 0,
                        )
                    if now >= close_ts and not w.evicted:
                        w.evicted = True  # drops from the stream once flat
                        w.wake()
                        window_close.pop(tkr, None)
                        awaiting_cleanup[tkr] = close_ts
                # Remove dead windows: flat (settled or never filled) and past
                # close by a grace period. A window still holding a position
                # stays until the settlement poll realizes it (position -> 0).
                for tkr, close_ts in list(awaiting_cleanup.items()):
                    st = risk.markets.get(tkr)
                    if st is not None and st.position != 0:
                        continue
                    if now < close_ts + 300:
                        continue
                    awaiting_cleanup.pop(tkr, None)
                    # Freeze the risk entry: a flat, closed window has nothing
                    # to settle, and without this marks_tick would write a mid
                    # row per minute for every window ever quoted (~864/day).
                    # cash stays in the MarketState, so realized PnL is intact.
                    if st is not None and not st.settled:
                        st.settled = True
                    w = workers.pop(tkr, None)
                    if w is not None:
                        try:
                            await w.stop()  # ends the run() task; orders already gone
                        except Exception:  # noqa: BLE001
                            log.exception("window cleanup stop failed for %s", tkr)
                    close_times.pop(tkr, None)
                await asyncio.sleep(1.0)

        async def spot_feed():
            """M4 (2026-08-06): defensive spot-jump pull. Polls Coinbase spot
            for mapped series; a move >= spot_jump_bps inside the window
            cancels that series' quotes for a cooloff. Budish et al: the slow
            maker's edge is refusing to be the stale quote — this feed exists
            to PULL, never to price (the pricing experiment already lost)."""
            import ssl

            import aiohttp
            import certifi

            ctx = ssl.create_default_context(cafile=certifi.where())
            hist: dict[str, list] = {s: [] for s in p.spot_products}
            last_emit: dict[str, float] = {}
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5),
                connector=aiohttp.TCPConnector(ssl=ctx),
                headers={"User-Agent": "bacchus-mm/0.1"},
            ) as session:
                while not stop_event.is_set():
                    for series, product in p.spot_products.items():
                        try:
                            async with session.get(
                                f"https://api.exchange.coinbase.com/products/{product}/ticker"
                            ) as r:
                                d = await r.json()
                            px = float(d.get("price") or 0)
                        except Exception:  # noqa: BLE001 — one venue hiccup must not kill M4
                            continue
                        if px <= 0:
                            continue
                        now = time.monotonic()
                        h = hist[series]
                        h.append((now, px))
                        del h[: max(0, len(h) - 60)]
                        bps = detect_jump(h, p.spot_jump_window_seconds, now)
                        if bps < p.spot_jump_bps:
                            continue
                        pulled = []
                        for tkr, w in list(workers.items()):
                            if (
                                tkr.startswith(series + "-")
                                and not w.evicted
                                and not w.close_reaped
                            ):
                                w.pulled_until = now + p.spot_jump_cooloff_seconds
                                w.wake()
                                pulled.append(tkr)
                        if pulled and now - last_emit.get(series, 0) >= 5:
                            last_emit[series] = now
                            events.emit(
                                "fifteen_spot_pull", ticker=pulled[0], series=series,
                                move_bps=round(bps, 1),
                                cooloff=p.spot_jump_cooloff_seconds,
                            )
                            log.info(
                                "spot jump %s: %.1f bps in %.0fs -> pulled %s for %.0fs",
                                series, bps, p.spot_jump_window_seconds,
                                ",".join(pulled), p.spot_jump_cooloff_seconds,
                            )
                    await asyncio.sleep(p.spot_poll_seconds)

        # 2026-08-07 stage 2: heartbeat shared with the stall watchdog THREAD,
        # which stack-samples the loop the moment this goes silent.
        import threading as _threading

        beat = {"t": time.monotonic()}
        start_stall_watchdog(_threading.get_ident(), beat)

        async def loop_lag_probe():
            """2026-08-07 stall investigation: the decisive discriminator.
            sleep(1) oversleeping means the LOOP itself was frozen (blocked
            stdout, a sync call) — network latency to Kalshi cannot cause
            this. Correlate loop_lag events with order_placement_unknown and
            health-probe failures to attribute stalls."""
            while not stop_event.is_set():
                t0 = time.monotonic()
                await asyncio.sleep(1.0)
                beat["t"] = time.monotonic()
                lag = beat["t"] - t0 - 1.0
                if lag > 0.5:
                    events.emit("loop_lag", lag_seconds=round(lag, 3))
                    log.warning("event-loop lag %.2fs (loop was blocked)", lag)

        async def risk_loop():
            last_kv_persist = time.monotonic()
            while not stop_event.is_set():
                await asyncio.sleep(5)
                health_state.note_event()
                pnl = risk.cumulative_pnl
                dd = risk.drawdown()
                events.record_pnl(pnl, risk.high_water, dd, risk.gross_contracts())
                if not dry_run and (
                    risk.new_high_water_since_load
                    or time.monotonic() - last_kv_persist >= 60
                ):
                    persist_pnl_marks(events, risk)
                    last_kv_persist = time.monotonic()
                reason = risk.should_halt()
                if reason and not risk.halted and not dry_run:
                    risk.halt(reason)
                    events.emit("halt", reason=reason, pnl=pnl, drawdown=dd)
                    log.error("KILL SWITCH: %s", reason)
                    try:
                        n = await ex.cancel_all_orders(tickers=managed_tickers(workers, risk))
                        log.error("kill switch canceled %d resting orders; bot is halted", n)
                    except Exception:  # noqa: BLE001
                        log.exception("cancel-all during halt failed — CHECK THE EXCHANGE UI")
                    stop_event.set()

        async def eventlog_flush_loop():
            while not stop_event.is_set():
                await asyncio.sleep(cfg.log_flush_seconds)
                try:
                    events.flush()
                except Exception:  # noqa: BLE001
                    log.exception("eventlog flush failed")

        async def marks_loop():
            # marks only — no close reaper here; pull_loop owns close handling
            # at seconds granularity.
            while not stop_event.is_set():
                await asyncio.sleep(cfg.marks_tick_seconds)
                try:
                    marks_tick(workers, risk, events, cfg.marks_tick_seconds)
                except Exception:  # noqa: BLE001
                    log.exception("marks tick failed")

        async def settlement_poll_loop():
            while not stop_event.is_set():
                await asyncio.sleep(p.settlement_poll_seconds)
                try:
                    await settlement_poll(ex, risk, events, close_times, workers)
                except Exception:  # noqa: BLE001
                    log.exception("settlement poll failed")

        _spawn(consume_stream(), "stream")
        _spawn(discovery_loop(), "discovery")
        _spawn(pull_loop(), "pull_loop")
        if p.spot_jump_bps and p.spot_products:
            _spawn(spot_feed(), "spot_feed")  # M4; runs in observe too (telemetry)
        _spawn(loop_lag_probe(), "loop_lag_probe")  # 2026-08-07 stall instrumentation
        _spawn(risk_loop(), "risk_loop")
        _spawn(eventlog_flush_loop(), "eventlog_flush")
        _spawn(marks_loop(), "marks_loop")
        if not dry_run:
            _spawn(settlement_poll_loop(), "settlement_poll")
            _spawn(
                reconcile_loop(
                    ex, workers, risk, events, gate, stop_event,
                    cfg.reconcile_seconds, cfg.sweep_cooloff_seconds,
                    ttl_seconds=p.order_ttl_seconds,
                ),
                "reconcile",
            )
        for t in list(workers):  # legacy wind-down workers created above
            _spawn(workers[t].run(), f"worker:{t}")

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
                managed = managed_tickers(workers, risk)
                remaining = await ex.cancel_all_orders(tickers=managed)
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
        if not dry_run:
            try:
                persist_pnl_marks(events, risk)
            except Exception:  # noqa: BLE001
                log.exception("failed to persist cumulative pnl on shutdown")
        events.close()
        if health_runner is not None:
            try:
                await health_runner.cleanup()
            except Exception:  # noqa: BLE001
                log.exception("health server cleanup failed")
        await ex.close()
