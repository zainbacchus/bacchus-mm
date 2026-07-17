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
import uuid
from pathlib import Path

from .config import Config
from .crossvenue import VenuePair, run_recorder
from .eventlog import EventLog
from .exchange.base import Fill
from .exchange.kalshi import KalshiAuth, KalshiExchange
from .marketmaker import MarketWorker, WorkerConfig
from .risk import RiskManager
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
        env=cfg.env, auth=auth, write_tokens_per_second=cfg.write_tokens_per_second
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
    events = EventLog(cfg.data_dir, session_id)
    risk = RiskManager(params=cfg.risk, state_dir=cfg.data_dir)

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

    try:
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
        for s in picks:
            risk.seed_position(s.market.ticker, positions.get(s.market.ticker, 0), s.market.mid)
        # Orphan positions (held in markets we no longer quote) stay marked so
        # PnL and the kill switch always see the whole book.
        for t, pos in positions.items():
            if t not in tickers:
                risk.seed_position(t, pos, None)

        if not dry_run:
            await ex.ensure_order_group(cfg.order_group_contracts_per_15s)
            stale = await ex.cancel_all_orders()  # no orphans from previous runs
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
            guard_evict_trips=cfg.guard_evict_trips,
        )
        for t in tickers:
            workers[t] = MarketWorker(t, ex, cfg.strategy, risk, events, wcfg, dry_run=dry_run)
        # Wind-down workers: every orphan position gets exit-only quotes until
        # flat — the bot never leaves inventory unmanaged (owner directive
        # 2026-07-16, after an evicted market's short ran 24c with no exit).
        for t, pos in positions.items():
            if t not in workers and pos != 0:
                workers[t] = MarketWorker(
                    t, ex, cfg.strategy, risk, events, wcfg,
                    dry_run=dry_run, reduce_only=True,
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

        def on_fill(f: Fill):
            w = workers.get(f.ticker)
            mid = w.current_mid() if w else None
            risk.on_fill(f.ticker, f.signed_count, f.yes_price)
            events.record_fill(
                f.ticker, f.trade_id, f.order_id, f.signed_count,
                f.yes_price, f.is_taker, mid, f.ts_ms,
            )
            if w:
                w.order_filled(f.order_id, abs(f.signed_count))
            log.info(
                "FILL %s %+d @ %.2f (taker=%s) pos=%d pnl=$%.2f",
                f.ticker, f.signed_count, f.yes_price, f.is_taker,
                risk.markets[f.ticker].position, risk.pnl(),
            )

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
                            sub.ticker, ex, cfg.strategy, risk, events, wcfg, dry_run=dry_run
                        )
                        risk.seed_position(sub.ticker, positions.get(sub.ticker, 0), sub.mid)
                        tasks.append(asyncio.create_task(workers[sub.ticker].run()))
                        events.emit(
                            "market_promoted", ticker=sub.ticker, replaces=t,
                            standby_remaining=len(standby),
                        )
                        log.info("promoted %s from standby (replacing evicted %s)", sub.ticker, t)
                        ex.request_resubscribe()
                        break
                    else:
                        log.warning("standby bench empty; %s not replaced", t)

        async def risk_loop():
            while not stop_event.is_set():
                await asyncio.sleep(5)
                pnl = risk.pnl()
                dd = risk.drawdown()
                events.record_pnl(pnl, risk.session_high, dd, risk.gross_contracts())
                reason = risk.should_halt()
                if reason and not risk.halted and not dry_run:
                    risk.halt(reason)
                    events.emit("halt", reason=reason, pnl=pnl, drawdown=dd)
                    log.error("KILL SWITCH: %s", reason)
                    try:
                        n = await ex.cancel_all_orders()
                        log.error("kill switch canceled %d resting orders; bot is halted", n)
                    except Exception:  # noqa: BLE001
                        log.exception("cancel-all during halt failed — CHECK THE EXCHANGE UI")
                    stop_event.set()

        tasks.append(asyncio.create_task(consume_stream()))
        tasks.append(asyncio.create_task(risk_loop()))
        tasks.append(asyncio.create_task(bench_loop()))
        tasks += [asyncio.create_task(w.run()) for w in workers.values()]

        # Cross-venue recorder rides along whenever pairs are configured — one
        # command ingests everything. `bacchus-mm crossvenue` still runs it alone.
        xv = cfg.raw.get("crossvenue", {}) or {}
        xv_pairs = [VenuePair.from_config(p) for p in xv.get("pairs", [])]
        if xv_pairs:
            log.info("cross-venue recorder attached: %d pairs", len(xv_pairs))
            tasks.append(
                asyncio.create_task(
                    run_recorder(xv_pairs, ex, events, float(xv.get("poll_seconds", 15)))
                )
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
                remaining = await ex.cancel_all_orders()
                resting = await ex.get_resting_orders()
                events.emit("session_stop", canceled=remaining, still_resting=len(resting))
                if resting:
                    log.error("%d orders STILL RESTING after shutdown — check the exchange UI", len(resting))
                else:
                    log.info("shutdown clean: no resting orders")
            except Exception:  # noqa: BLE001
                log.exception("shutdown cancel-all failed — CHECK THE EXCHANGE UI")
        else:
            events.emit("session_stop", canceled=0, still_resting=0)
        events.close()
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
    events = EventLog(cfg.data_dir, session_id)
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
    events = EventLog(cfg.data_dir, f"selftest-{time.strftime('%Y%m%d-%H%M%S')}")
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
    risk = RiskManager(params=cfg.risk, state_dir=cfg.data_dir)
    reason = risk.check_halt_file()
    if reason is None:
        print("no HALTED marker present")
        return
    risk.clear_halt()
    print(f"cleared halt: {reason}")


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
