"""Polymarket read-only data client (Phase A of cross-venue support).

Market data is fully public — no credentials:
  Gamma API  https://gamma-api.polymarket.com  (market metadata, token ids)
  CLOB API   https://clob.polymarket.com       (/book, /midpoint per outcome token)

Trading (Phase C, not implemented) would use the authenticated CLOB endpoints;
see ROADMAP.md and scripts/add-polymarket-key.sh for the credential slots.
"""

from __future__ import annotations

import json
import ssl
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import aiohttp
import certifi

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"


@dataclass
class PolymarketMarket:
    slug: str
    question: str
    outcomes: list[str]
    token_ids: list[str]  # aligned with outcomes
    tick_size: Decimal
    volume_24h: Decimal
    end_date: str

    def token_for(self, outcome: str) -> str:
        for name, token in zip(self.outcomes, self.token_ids):
            if name.lower() == outcome.lower():
                return token
        raise KeyError(f"outcome {outcome!r} not in {self.outcomes} for {self.slug}")


@dataclass
class PolymarketTop:
    token_id: str
    bid: Optional[Decimal]
    bid_size: Decimal
    ask: Optional[Decimal]
    ask_size: Decimal
    ts_ms: int

    @property
    def mid(self) -> Optional[Decimal]:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2


def parse_market(m: dict) -> PolymarketMarket:
    # Gamma encodes list fields as JSON strings.
    outcomes = json.loads(m.get("outcomes") or "[]")
    token_ids = json.loads(m.get("clobTokenIds") or "[]")
    return PolymarketMarket(
        slug=m.get("slug", ""),
        question=m.get("question", ""),
        outcomes=outcomes,
        token_ids=token_ids,
        tick_size=Decimal(str(m.get("orderPriceMinTickSize") or "0.01")),
        volume_24h=Decimal(str(m.get("volume24hr") or 0)),
        end_date=m.get("endDate", ""),
    )


def parse_book_top(book: dict) -> PolymarketTop:
    """CLOB /book returns full depth; bids best = highest price, asks best = lowest."""
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = max(bids, key=lambda lvl: Decimal(lvl["price"]), default=None)
    best_ask = min(asks, key=lambda lvl: Decimal(lvl["price"]), default=None)
    return PolymarketTop(
        token_id=book.get("asset_id", ""),
        bid=Decimal(best_bid["price"]) if best_bid else None,
        bid_size=Decimal(best_bid["size"]) if best_bid else Decimal(0),
        ask=Decimal(best_ask["price"]) if best_ask else None,
        ask_size=Decimal(best_ask["size"]) if best_ask else Decimal(0),
        ts_ms=int(book.get("timestamp") or time.time() * 1000),
    )


class PolymarketData:
    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "bacchus-mm/0.1"},
                connector=aiohttp.TCPConnector(ssl=ssl_ctx),
            )
        return self._session

    async def _get(self, url: str, params: Optional[dict] = None):
        session = await self._http()
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_market(self, slug: str) -> PolymarketMarket:
        data = await self._get(f"{GAMMA_URL}/markets", {"slug": slug})
        if not data:
            raise KeyError(f"no Polymarket market with slug {slug!r}")
        return parse_market(data[0])

    async def find_markets(self, query: str, limit: int = 300) -> list[PolymarketMarket]:
        """Client-side text search over the most active open markets — a helper for
        building the cross-venue mapping table, not an exhaustive index."""
        data = await self._get(
            f"{GAMMA_URL}/markets",
            {"closed": "false", "order": "volume24hr", "ascending": "false", "limit": limit},
        )
        needle = query.lower()
        return [parse_market(m) for m in data if needle in (m.get("question") or "").lower()]

    async def get_top(self, token_id: str) -> PolymarketTop:
        book = await self._get(f"{CLOB_URL}/book", {"token_id": token_id})
        return parse_book_top(book)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
