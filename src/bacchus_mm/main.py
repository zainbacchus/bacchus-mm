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
        auth = KalshiAuth.from_files(creds.key_id, creds.private_key_path)
    elif need_auth:
        sys.exit(
            "Missing credentials: set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH "
            "(see .env.example). Demo keys come from demo.kalshi.co account settings."
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
        picks = select_markets(markets, cfg.selector)
        if not picks:
            sys.exit("Selector found no eligible markets; try `bacchus-mm markets` and loosen filters.")
        tickers = [s.market.ticker for s in picks]

        balance = await ex.get_balance()
        positions = await ex.get_positions()
        for s in picks:
            risk.seed_position(s.market.ticker, positions.get(s.market.ticker, 0), s.market.mid)

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
        )
        for t in tickers:
            workers[t] = MarketWorker(t, ex, cfg.strategy, risk, events, wcfg, dry_run=dry_run)

        def on_book_top(top):
            w = workers.get(top.ticker)
            if w:
                w.on_book_top(top)

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

        async def consume_stream():
            async for _ in ex.stream(tickers, on_book_top, on_fill):
                pass

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
        tasks += [asyncio.create_task(w.run()) for w in workers.values()]

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
    sub.add_parser("halt-clear", help="acknowledge a kill-switch halt")
    an = sub.add_parser("analyze", help="log analysis reports")
    an.add_argument("report", nargs="?", default="summary",
                    choices=["summary", "markouts", "quotes", "incidents"])
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
    elif args.command == "cancel-all":
        asyncio.run(cmd_cancel_all(cfg))
    elif args.command == "halt-clear":
        cmd_halt_clear(cfg)
    elif args.command == "analyze":
        from .analyze import run_report

        run_report(cfg.data_dir, args.report, args.hours)


if __name__ == "__main__":
    cli()
