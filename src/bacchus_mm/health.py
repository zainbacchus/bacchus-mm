"""Health endpoint for container deployment (2026-07-17, DEPLOY).

fly.io top-level machine checks (fly.toml [checks]) need an HTTP endpoint to
poll; this is a minimal aiohttp server riding the bot's own asyncio loop — no
threads, no extra dependencies (aiohttp is already the exchange client).

Liveness signal: HealthState.note_event() is called from EventLog.emit (every
structured event) and from the 5s risk_loop heartbeat, so last_event_age_s is
"how long since the bot demonstrably did anything". A wedged event loop stops
the risk_loop heartbeat and the endpoint goes 503; a dead task trips
supervise() -> clean shutdown -> fly's restart policy restarts the process.

Security: the payload is a whitelist of coarse status fields. No key ids, no
positions detail, no per-market exposure — /health binds 0.0.0.0 inside fly's
private network and must be safe to leak there.
"""

from __future__ import annotations

import importlib.metadata
import logging
import time
from typing import Any, Optional

from aiohttp import web

log = logging.getLogger(__name__)

# 2026-07-17 (DEPLOY): 5 minutes without a single event/heartbeat means the
# loop is wedged (risk_loop ticks every 5s in a healthy process).
STALE_AFTER_SECONDS = 300.0


def _version() -> str:
    try:
        return importlib.metadata.version("bacchus-mm")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


class HealthState:
    """Mutable liveness snapshot read by the /health handler.

    Duck-typed against RiskManager (halted, halt_file, cumulative_pnl) and the
    workers dict (w.evicted) so tests can stub them with SimpleNamespace."""

    def __init__(self, mode: str, live: bool, risk: Any, workers: dict):
        self.mode = mode
        self.live = live
        self._risk = risk
        self._workers = workers
        self._started_mono = time.monotonic()
        self._last_event_mono = self._started_mono

    def note_event(self) -> None:
        self._last_event_mono = time.monotonic()

    def snapshot(self) -> dict:
        age = time.monotonic() - self._last_event_mono
        # The HALTED marker file counts even before risk.halt() runs in this
        # process (e.g. a halt from a previous session on the same volume).
        halted = bool(self._risk.halted or self._risk.halt_file.exists())
        return {
            "mode": self.mode,
            "live": self.live,
            "halted": halted,
            "uptime_s": round(time.monotonic() - self._started_mono, 1),
            "last_event_age_s": round(age, 1),
            "markets_active": sum(
                1 for w in self._workers.values() if not getattr(w, "evicted", False)
            ),
            "cumulative_pnl": float(self._risk.cumulative_pnl),
            "version": _version(),
        }

    def healthy(self) -> bool:
        snap = self.snapshot()
        return not snap["halted"] and snap["last_event_age_s"] <= STALE_AFTER_SECONDS


async def start_health_server(
    state: HealthState, port: int, host: str = "0.0.0.0"
) -> web.AppRunner:
    """Start GET /health on the running loop. Caller must await runner.cleanup()
    on shutdown. 200 when healthy, 503 when halted or stale — the body is the
    same JSON either way so the check output stays debuggable."""

    async def handle(_request: web.Request) -> web.Response:
        snap = state.snapshot()
        status = (
            200
            if not snap["halted"] and snap["last_event_age_s"] <= STALE_AFTER_SECONDS
            else 503
        )
        return web.json_response(snap, status=status)

    app = web.Application()
    app.router.add_get("/health", handle)
    runner = web.AppRunner(app, access_log=None)  # quiet: polled every 30s forever
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner


def bound_port(runner: web.AppRunner) -> Optional[int]:
    """Actual bound port (tests pass port=0 for an ephemeral one)."""
    for site in runner.sites:
        server = getattr(site, "_server", None)
        if server and server.sockets:
            return server.sockets[0].getsockname()[1]
    return None
