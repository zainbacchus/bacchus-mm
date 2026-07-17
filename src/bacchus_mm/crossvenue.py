"""Cross-venue divergence recorder (Phase A).

Polls mapped Kalshi/Polymarket market pairs and logs both venues' top-of-book
side by side. The resulting `venue_marks` table answers the questions that
gate Phase B (fair-value anchoring) and Phase C (cross-venue execution):
how often do the venues disagree, by how much, and for how long?

Pairs are declared in config.local.yaml — mapping is deliberately manual,
because deciding that two contracts are semantically identical (same
resolution source, cutoff, definition) is a human judgment:

    crossvenue:
      poll_seconds: 15
      pairs:
        - kalshi: KXCPIYOY-26JUL-T3.7
          polymarket_slug: will-cpi-inflation-be-above-3-7-in-july-2026
          polymarket_outcome: "Yes"   # optional, default first outcome
          invert: false               # true if PM outcome == Kalshi NO side
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .eventlog import EventLog
from .exchange.kalshi import KalshiExchange, _dec
from .exchange.polymarket import PolymarketData

log = logging.getLogger(__name__)


@dataclass
class VenuePair:
    kalshi_ticker: str
    polymarket_slug: str
    polymarket_outcome: Optional[str] = None  # default: first listed outcome
    invert: bool = False  # PM price tracks Kalshi NO side -> compare 1 - pm

    @classmethod
    def from_config(cls, raw: dict) -> "VenuePair":
        return cls(
            kalshi_ticker=raw["kalshi"],
            polymarket_slug=raw["polymarket_slug"],
            polymarket_outcome=raw.get("polymarket_outcome"),
            invert=bool(raw.get("invert", False)),
        )


async def _kalshi_quote(
    kalshi: KalshiExchange, ticker: str
) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """Best yes bid/ask from the public GetMarkets endpoint (no auth needed)."""
    data = await kalshi._request("GET", "/markets", params={"tickers": ticker}, authed=False)
    markets = data.get("markets") or []
    if not markets:
        return None, None
    m = markets[0]
    return _dec(m.get("yes_bid_dollars")), _dec(m.get("yes_ask_dollars"))


async def run_recorder(
    pairs: list[VenuePair],
    kalshi: KalshiExchange,
    events: EventLog,
    poll_seconds: float = 15.0,
) -> None:
    pm = PolymarketData()
    tokens: dict[str, str] = {}
    try:
        resolved: list[VenuePair] = []
        for pair in pairs:
            # Round 2 (adversarial): under fail-stop supervision, an unguarded
            # resolution failure here (Gamma outage, renamed slug/outcome)
            # would take down the whole live bot. Skip bad pairs instead.
            try:
                market = await pm.get_market(pair.polymarket_slug)
                outcome = pair.polymarket_outcome or market.outcomes[0]
                tokens[pair.polymarket_slug] = market.token_for(outcome)
            except Exception as e:  # noqa: BLE001
                log.warning("crossvenue pair %s failed to resolve: %r — skipping",
                            pair.polymarket_slug, e)
                continue
            resolved.append(pair)
            log.info(
                "pair %s <-> %s [%s] (%s)",
                pair.kalshi_ticker, pair.polymarket_slug, outcome, market.question,
            )
        pairs = resolved
        if not pairs:
            log.warning("crossvenue: no pairs resolved; recorder idle this session")
            return
        while True:
            for pair in pairs:
                try:
                    k_bid, k_ask = await _kalshi_quote(kalshi, pair.kalshi_ticker)
                    top = await pm.get_top(tokens[pair.polymarket_slug])
                    pm_bid, pm_ask = top.bid, top.ask
                    if pair.invert:
                        pm_bid, pm_ask = (
                            (Decimal(1) - top.ask) if top.ask is not None else None,
                            (Decimal(1) - top.bid) if top.bid is not None else None,
                        )
                    k_mid = (k_bid + k_ask) / 2 if k_bid is not None and k_ask is not None else None
                    pm_mid = (pm_bid + pm_ask) / 2 if pm_bid is not None and pm_ask is not None else None
                    divergence = (pm_mid - k_mid) if k_mid is not None and pm_mid is not None else None
                    events.record_venue_mark(
                        pair.kalshi_ticker, pair.polymarket_slug,
                        k_bid, k_ask, pm_bid, pm_ask, divergence,
                    )
                    if divergence is not None and abs(divergence) >= Decimal("0.03"):
                        log.info(
                            "divergence %s: kalshi %.3f vs polymarket %.3f (%+.3f)",
                            pair.kalshi_ticker, k_mid, pm_mid, divergence,
                        )
                except Exception as e:  # noqa: BLE001 — one bad pair must not stop the rest
                    log.warning("crossvenue poll failed for %s: %r", pair.kalshi_ticker, e)
                    events.emit(
                        "error", ticker=pair.kalshi_ticker, where="crossvenue", error=repr(e)
                    )
            await asyncio.sleep(poll_seconds)
    finally:
        await pm.close()
