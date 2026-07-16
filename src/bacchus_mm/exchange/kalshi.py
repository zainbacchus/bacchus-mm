"""Kalshi exchange adapter: RSA-PSS auth, REST v2 order endpoints, websocket market data.

Built against the published specs (July 2026):
  REST:  https://docs.kalshi.com/openapi.yaml   (order ops via /portfolio/events/orders, V2 shape)
  WS:    https://docs.kalshi.com/asyncapi.yaml  (orderbook_delta + fill channels)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import ssl
import time
import uuid
from decimal import Decimal
from typing import AsyncIterator, Callable, Optional

import aiohttp
import certifi
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .base import BookTop, ExchangeAdapter, Fill, MarketInfo, Order, Side

log = logging.getLogger(__name__)

ENVS = {
    "demo": {
        "rest": "https://demo-api.kalshi.co/trade-api/v2",
        "ws": "wss://demo-api.kalshi.co/trade-api/ws/v2",
    },
    "prod": {
        "rest": "https://api.elections.kalshi.com/trade-api/v2",
        "ws": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    },
}


def _dec(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    return Decimal(str(v))


def _fmt_price(p: Decimal) -> str:
    return f"{p:.4f}"


def _fmt_count(c: int) -> str:
    return f"{c}.00"


class KalshiAuth:
    """Signs requests per Kalshi's API-key scheme: RSA-PSS(SHA256) over ts+method+path."""

    def __init__(self, key_id: str, private_key_pem: bytes):
        self.key_id = key_id
        self._key = serialization.load_pem_private_key(private_key_pem, password=None)

    @classmethod
    def from_files(cls, key_id: str, private_key_path: str) -> "KalshiAuth":
        with open(private_key_path, "rb") as f:
            return cls(key_id, f.read())

    def headers(self, method: str, path: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        message = f"{ts}{method.upper()}{path}".encode()
        signature = self._key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }


class TokenBucket:
    """Client-side write-budget guard (Kalshi Basic tier: 100 write tokens/s)."""

    def __init__(self, tokens_per_second: float, capacity: Optional[float] = None):
        self.rate = tokens_per_second
        self.capacity = capacity or tokens_per_second
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                await asyncio.sleep((cost - self._tokens) / self.rate)


class OrderBook:
    """Local yes/no bid book maintained from websocket snapshot + deltas."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        self.yes_bids: dict[Decimal, Decimal] = {}
        self.no_bids: dict[Decimal, Decimal] = {}
        self.ts_ms: int = 0

    def apply_snapshot(self, msg: dict) -> None:
        self.yes_bids = {Decimal(p): Decimal(c) for p, c in msg.get("yes_dollars_fp") or []}
        self.no_bids = {Decimal(p): Decimal(c) for p, c in msg.get("no_dollars_fp") or []}
        self.ts_ms = msg.get("ts_ms") or int(time.time() * 1000)

    def apply_delta(self, msg: dict) -> None:
        book = self.yes_bids if msg["side"] == "yes" else self.no_bids
        price = Decimal(msg["price_dollars"])
        qty = book.get(price, Decimal(0)) + Decimal(msg["delta_fp"])
        if qty <= 0:
            book.pop(price, None)
        else:
            book[price] = qty
        self.ts_ms = msg.get("ts_ms") or int(time.time() * 1000)

    def top(self) -> BookTop:
        bid = max(self.yes_bids) if self.yes_bids else None
        best_no = max(self.no_bids) if self.no_bids else None
        ask = (Decimal(1) - best_no) if best_no is not None else None
        return BookTop(
            ticker=self.ticker,
            bid=bid,
            bid_size=int(self.yes_bids[bid]) if bid is not None else 0,
            ask=ask,
            ask_size=int(self.no_bids[best_no]) if best_no is not None else 0,
            ts_ms=self.ts_ms,
        )


def fill_signed_count(side: str, action: str, count: int) -> int:
    """Signed yes-equivalent delta for a fill (buy no == sell yes at 1-p)."""
    long_yes = (side == "yes") == (action == "buy")
    return count if long_yes else -count


class KalshiExchange(ExchangeAdapter):
    def __init__(
        self,
        env: str,
        auth: Optional[KalshiAuth],
        rest_url: Optional[str] = None,
        ws_url: Optional[str] = None,
        write_tokens_per_second: float = 50.0,
    ):
        if env not in ENVS:
            raise ValueError(f"env must be one of {list(ENVS)}, got {env!r}")
        self.env = env
        self.auth = auth
        self.rest_url = rest_url or ENVS[env]["rest"]
        self.ws_url = ws_url or ENVS[env]["ws"]
        self._session: Optional[aiohttp.ClientSession] = None
        self._write_bucket = TokenBucket(write_tokens_per_second)
        self.order_group_id: Optional[str] = None
        self._resubscribe = False
        self.trading_paused_until = 0.0

    # ---------------------------------------------------------------- REST

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "bacchus-mm/0.1"},
                connector=aiohttp.TCPConnector(ssl=ssl_ctx),
            )
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        authed: bool = True,
        retries: int = 3,
    ) -> dict:
        """path is relative to /trade-api/v2, e.g. '/markets'."""
        base_path = self.rest_url.split("//", 1)[1]
        base_path = "/" + base_path.split("/", 1)[1]  # e.g. /trade-api/v2
        url = f"{self.rest_url}{path}"
        session = await self._http()
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            headers = {}
            if authed:
                if self.auth is None:
                    raise RuntimeError("This call requires API credentials; none configured.")
                headers = self.auth.headers(method, f"{base_path}{path}")
            try:
                async with session.request(
                    method, url, params=params, json=body, headers=headers
                ) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        text = await resp.text()
                        last_err = RuntimeError(f"{method} {path} -> {resp.status}: {text[:300]}")
                        await asyncio.sleep(0.5 * 2**attempt)
                        continue
                    if resp.status >= 400:
                        text = await resp.text()
                        raise KalshiApiError(resp.status, f"{method} {path}: {text[:500]}")
                    if resp.status == 204:
                        return {}
                    return await resp.json()
            except aiohttp.ClientError as e:
                last_err = e
                await asyncio.sleep(0.5 * 2**attempt)
        raise last_err or RuntimeError(f"{method} {path} failed")

    async def list_markets(self) -> list[MarketInfo]:
        """Open events with nested markets; category comes from the event/series level."""
        out: list[MarketInfo] = []
        cursor = None
        for _ in range(40):  # hard page cap
            params = {"status": "open", "with_nested_markets": "true", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = await self._request("GET", "/events", params=params, authed=False)
            for ev in data.get("events", []):
                category = ev.get("category") or ""
                for m in ev.get("markets") or []:
                    if m.get("status") != "active" and m.get("status") != "open":
                        continue
                    out.append(
                        MarketInfo(
                            ticker=m["ticker"],
                            event_ticker=ev.get("event_ticker", ""),
                            series_ticker=ev.get("series_ticker")
                            or ev.get("event_ticker", "").split("-")[0],
                            title=m.get("title") or ev.get("title") or "",
                            category=category,
                            close_time=m.get("close_time", ""),
                            yes_bid=_dec(m.get("yes_bid_dollars")),
                            yes_ask=_dec(m.get("yes_ask_dollars")),
                            volume_24h=_dec(m.get("volume_24h_fp")) or Decimal(0),
                            open_interest=_dec(m.get("open_interest_fp")) or Decimal(0),
                            raw=m,
                        )
                    )
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    async def get_24h_mid_range(self, series_ticker: str, ticker: str) -> Optional[Decimal]:
        """Realized 24h swing of the bid/ask midpoint from hourly candles — the
        falling-knife signal (a net-move check misses round trips: one market
        swung 66c in a day and netted -10c). None when history is unavailable."""
        now = int(time.time())
        try:
            data = await self._request(
                "GET",
                f"/series/{series_ticker}/markets/{ticker}/candlesticks",
                params={"period_interval": 60, "start_ts": now - 86400, "end_ts": now},
                authed=False,
            )
        except Exception:  # noqa: BLE001 — screening signal, never fatal
            return None
        mids_hi, mids_lo = [], []
        for c in data.get("candlesticks", []):
            bid, ask = c.get("yes_bid") or {}, c.get("yes_ask") or {}
            try:
                hi = (Decimal(bid["high_dollars"]) + Decimal(ask["high_dollars"])) / 2
                lo = (Decimal(bid["low_dollars"]) + Decimal(ask["low_dollars"])) / 2
            except (KeyError, TypeError):
                continue
            mids_hi.append(hi)
            mids_lo.append(lo)
        if not mids_hi:
            return None
        return max(mids_hi) - min(mids_lo)

    async def create_order(
        self,
        ticker: str,
        side: Side,
        price: Decimal,
        count: int,
        client_order_id: str,
        expiration_seconds: Optional[int] = None,
    ) -> Order:
        await self._write_bucket.acquire(10)
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": side.value,
            "count": _fmt_count(count),
            "price": _fmt_price(price),
            "time_in_force": "good_till_canceled",
            "post_only": True,  # never take; a crossing quote is a bug, reject it
            "self_trade_prevention_type": "maker",
            "cancel_order_on_pause": True,
        }
        if expiration_seconds:
            body["expiration_time"] = int(time.time()) + expiration_seconds
        if self.order_group_id:
            body["order_group_id"] = self.order_group_id
        data = await self._request("POST", "/portfolio/events/orders", body=body)
        o = data.get("order", data)
        return Order(
            order_id=o.get("order_id", ""),
            client_order_id=client_order_id,
            ticker=ticker,
            side=side,
            price=price,
            count=count,
            status=o.get("status", "resting"),
        )

    async def cancel_order(self, order_id: str) -> None:
        await self._write_bucket.acquire(2)
        try:
            await self._request("DELETE", f"/portfolio/events/orders/{order_id}")
        except KalshiApiError as e:
            if e.status == 404:  # already gone (filled or expired) — that's fine
                return
            raise

    async def cancel_all_orders(self, tickers: Optional[list[str]] = None) -> int:
        orders = await self.get_resting_orders()
        if tickers is not None:
            orders = [o for o in orders if o.ticker in tickers]
        for o in orders:
            await self.cancel_order(o.order_id)
        return len(orders)

    async def get_resting_orders(self) -> list[Order]:
        out: list[Order] = []
        cursor = None
        while True:
            params = {"status": "resting", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = await self._request("GET", "/portfolio/orders", params=params)
            for o in data.get("orders", []):
                side = o.get("side")
                action = o.get("action")
                # Legacy order objects use side=yes/no + action; V2 uses bid/ask.
                if side in ("bid", "ask"):
                    book_side = Side(side)
                    price = _dec(o.get("price_dollars")) or _dec(o.get("yes_price_dollars"))
                else:
                    long_yes = (side == "yes") == (action == "buy")
                    book_side = Side.BID if long_yes else Side.ASK
                    yp = _dec(o.get("yes_price_dollars"))
                    if yp is None and o.get("yes_price") is not None:
                        yp = Decimal(o["yes_price"]) / 100
                    price = yp
                remaining = o.get("remaining_count_fp") or o.get("remaining_count") or 0
                out.append(
                    Order(
                        order_id=o.get("order_id", ""),
                        client_order_id=o.get("client_order_id", ""),
                        ticker=o.get("ticker", ""),
                        side=book_side,
                        price=price or Decimal(0),
                        count=int(Decimal(str(remaining))),
                        status="resting",
                    )
                )
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    async def get_positions(self) -> dict[str, int]:
        out: dict[str, int] = {}
        cursor = None
        while True:
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = await self._request("GET", "/portfolio/positions", params=params)
            for p in data.get("market_positions", []):
                pos = p.get("position_fp") or p.get("position") or 0
                signed = int(Decimal(str(pos)))
                if signed:
                    out[p["ticker"]] = signed
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    async def get_balance(self) -> Decimal:
        data = await self._request("GET", "/portfolio/balance")
        if "balance_dollars" in data:
            return Decimal(str(data["balance_dollars"]))
        return Decimal(data.get("balance", 0)) / 100

    async def ensure_order_group(self, contracts_per_15s: int) -> Optional[str]:
        """Exchange-side runaway guard: if the group fills more than N contracts in a
        rolling 15s window, Kalshi cancels every order in it. Best-effort."""
        try:
            data = await self._request(
                "POST",
                "/portfolio/order_groups/create",
                body={"contracts_limit": contracts_per_15s},
            )
            gid = data.get("order_group_id") or (data.get("order_group") or {}).get(
                "order_group_id"
            )
            self.order_group_id = gid
            return gid
        except Exception as e:  # noqa: BLE001 — safety feature, never fatal
            log.warning("order group creation failed (continuing without): %s", e)
            return None

    # ------------------------------------------------------------ WEBSOCKET

    def request_resubscribe(self) -> None:
        """Ask the running stream to reconnect with a fresh get_tickers() —
        used when the active market set changes mid-session."""
        self._resubscribe = True

    async def stream(
        self,
        get_tickers: Callable[[], list[str]],
        on_book_top: Callable[[BookTop], None],
        on_fill: Callable[[Fill], None],
    ) -> AsyncIterator[None]:
        """Maintains books from orderbook_delta and dispatches private fills.
        Reconnects forever; caller cancels the task to stop."""
        if self.auth is None:
            raise RuntimeError("websocket requires API credentials")
        self._resubscribe = False
        backoff = 1.0
        while True:
            tickers = get_tickers()
            books: dict[str, OrderBook] = {t: OrderBook(t) for t in tickers}
            try:
                session = await self._http()
                headers = self.auth.headers("GET", "/trade-api/ws/v2")
                async with session.ws_connect(self.ws_url, headers=headers, heartbeat=10) as ws:
                    await ws.send_json(
                        {
                            "id": 1,
                            "cmd": "subscribe",
                            "params": {
                                "channels": ["orderbook_delta"],
                                "market_tickers": tickers,
                            },
                        }
                    )
                    await ws.send_json(
                        {"id": 2, "cmd": "subscribe", "params": {"channels": ["fill"]}}
                    )
                    backoff = 1.0
                    seqs: dict[int, int] = {}
                    async for raw in ws:
                        if self._resubscribe:
                            self._resubscribe = False
                            log.info("resubscribing websocket with updated market set")
                            raise _ResubscribeRequested()
                        if raw.type != aiohttp.WSMsgType.TEXT:
                            continue
                        msg = json.loads(raw.data)
                        mtype = msg.get("type")
                        sid, seq = msg.get("sid"), msg.get("seq")
                        if seq is not None and sid is not None:
                            expected = seqs.get(sid)
                            if expected is not None and seq != expected + 1:
                                log.warning("ws seq gap on sid=%s (%s -> %s); resyncing", sid, expected, seq)
                                raise ConnectionResetError("sequence gap")
                            seqs[sid] = seq
                        if mtype == "orderbook_snapshot":
                            m = msg["msg"]
                            book = books.get(m["market_ticker"])
                            if book:
                                book.apply_snapshot(m)
                                on_book_top(book.top())
                        elif mtype == "orderbook_delta":
                            m = msg["msg"]
                            book = books.get(m["market_ticker"])
                            if book:
                                book.apply_delta(m)
                                on_book_top(book.top())
                        elif mtype == "fill":
                            m = msg["msg"]
                            count = int(Decimal(str(m.get("count_fp") or m.get("count") or 0)))
                            yp = _dec(m.get("yes_price_dollars"))
                            if yp is None and m.get("yes_price") is not None:
                                yp = Decimal(m["yes_price"]) / 100
                            on_fill(
                                Fill(
                                    trade_id=m.get("trade_id", ""),
                                    order_id=m.get("order_id", ""),
                                    ticker=m.get("market_ticker", ""),
                                    signed_count=fill_signed_count(
                                        m.get("side", "yes"), m.get("action", "buy"), count
                                    ),
                                    yes_price=yp or Decimal(0),
                                    is_taker=bool(m.get("is_taker")),
                                    ts_ms=m.get("ts_ms") or int(time.time() * 1000),
                                    raw=m,
                                )
                            )
                        elif mtype == "error":
                            log.error("ws error message: %s", msg)
                        yield
            except asyncio.CancelledError:
                raise
            except _ResubscribeRequested:
                continue  # immediate reconnect with fresh tickers, no backoff
            except Exception as e:  # noqa: BLE001 — reconnect on any transport error
                log.warning("ws disconnected (%s); reconnecting in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class _ResubscribeRequested(Exception):
    """Internal signal: reconnect the websocket with an updated ticker set."""


class KalshiApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def new_client_order_id() -> str:
    return str(uuid.uuid4())
