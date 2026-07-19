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
from email.utils import parsedate_to_datetime
from typing import AsyncIterator, Callable, Optional

import aiohttp
import certifi
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .base import BookTop, ExchangeAdapter, Fill, MarketInfo, MarketLifecycle, Order, Side
from ..fees import FeeSchedule, compute_fee

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


def skew_from_date_header(date_header: str, now: Optional[float] = None) -> float:
    """Local minus server clock in seconds, from an HTTP Date header.
    Positive means the local clock is AHEAD of the server."""
    now = time.time() if now is None else now
    server_ts = parsedate_to_datetime(date_header).timestamp()
    return now - server_ts


async def rest_clock_skew_seconds(rest_url: str, timeout: float = 10.0) -> float:
    """2026-07-17 (DEPLOY): measure local-vs-Kalshi clock skew before trading.
    RSA-PSS auth signs with local-ms timestamps, so a skewed VPS clock fails
    auth opaquely (opaque 401s). Any unauthenticated GET against the REST base
    returns a Date header — even a 404 carries one, so the status is ignored.
    Raises on network failure; the caller decides (startup treats it as
    advisory-only, never fatal)."""
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout),
        connector=aiohttp.TCPConnector(ssl=ssl_ctx),
    ) as session:
        async with session.get(rest_url) as resp:
            date_header = resp.headers.get("Date")
    if not date_header:
        raise RuntimeError(f"no Date header from {rest_url}")
    return skew_from_date_header(date_header)


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


def _dispatch(callback: Callable, payload) -> None:
    """Invoke a stream callback with bug isolation (2026-07-17, H4): an
    exception in caller code (fill/book handlers — DB writes, worker
    bookkeeping) must NOT reach the transport error handler, where it would
    be misreported as a ws disconnect and trigger a pointless
    reconnect/resync. Log loudly via stdlib logging; keep the connection."""
    try:
        callback(payload)
    except Exception:  # noqa: BLE001
        log.exception(
            "stream callback %s raised — callback bug, connection kept",
            getattr(callback, "__name__", repr(callback)),
        )


class KalshiExchange(ExchangeAdapter):
    def __init__(
        self,
        env: str,
        auth: Optional[KalshiAuth],
        rest_url: Optional[str] = None,
        ws_url: Optional[str] = None,
        write_tokens_per_second: float = 50.0,
        fee_schedule: Optional[FeeSchedule] = None,
        ws_recv_timeout_seconds: float = 30.0,
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
        # 2026-07-17 (M2): max wait on a single ws receive. The resubscribe
        # flag is only checked between messages, so on a quiet book a bench
        # promotion could stay unsubscribed for hours; a receive timeout makes
        # the flag responsive within ws_recv_timeout_seconds of silence.
        self._ws_recv_timeout = ws_recv_timeout_seconds
        # 2026-07-17 (M7): used to estimate a fill's fee when the ws payload
        # doesn't carry the exchange's own fee_cost.
        self.fee_schedule = fee_schedule
        # 2026-07-17 (C1): the exchange-global trading_paused_until backoff is
        # gone — per-market suspension (worker) + sweep cooloff (QuotingGate)
        # replaced it; see marketmaker.py.

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
                            previous_price=_dec(m.get("previous_price_dollars")),
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
        post_only: bool = True,
    ) -> Order:
        await self._write_bucket.acquire(10)
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": side.value,
            "count": _fmt_count(count),
            "price": _fmt_price(price),
            "time_in_force": "good_till_canceled",
            # 2026-07-17 (M4): post_only=False exists ONLY for the wind-down
            # cross_1tick escalation (owner-gated, default off) — every other
            # caller keeps the post-only invariant; a crossing quote is a bug.
            "post_only": post_only,
            "self_trade_prevention_type": "maker",
            "cancel_order_on_pause": True,
        }
        if expiration_seconds:
            body["expiration_time"] = int(time.time()) + expiration_seconds
        if self.order_group_id:
            body["order_group_id"] = self.order_group_id
        # 2026-07-17 (M6): create-order POSTs are NOT retried. Kalshi's
        # OpenAPI spec (https://docs.kalshi.com/openapi.yaml, fetched
        # 2026-07-17) documents idempotency only for transfers
        # (client_transfer_id); duplicate client_order_id rejection on order
        # create is NOT documented anywhere, so a retry after an ambiguous
        # failure (timeout/5xx) could double-place. Failure handling instead:
        # the worker reconciles by client_order_id before re-placing (see
        # MarketWorker._reconcile's pending-adopt path).
        data = await self._request("POST", "/portfolio/events/orders", body=body, retries=0)
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

    async def get_resting_orders(self, ticker: Optional[str] = None) -> list[Order]:
        out: list[Order] = []
        cursor = None
        while True:
            params = {"status": "resting", "limit": 200}
            if ticker is not None:
                params["ticker"] = ticker
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

    async def get_market_status(self, ticker: str) -> Optional[MarketLifecycle]:
        """GET /markets/{ticker} lifecycle state (2026-07-17, M3). Kalshi's
        lifecycle is open/active -> closed -> determined -> finalized; `result`
        ("yes"/"no") is populated at determination. Settlement accounting keys
        off determined/finalized with a result — the outcome is economically
        locked at determination (finalized only times the cash movement)."""
        try:
            data = await self._request("GET", f"/markets/{ticker}", authed=False)
        except Exception as e:  # noqa: BLE001 — a poll must never kill its loop
            log.warning("get_market_status %s failed: %s", ticker, e)
            return None
        m = data.get("market", data)
        return MarketLifecycle(
            ticker=ticker,
            status=m.get("status", ""),
            result=m.get("result", ""),
            close_time=m.get("close_time", ""),
        )

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
                    while True:
                        # 2026-07-17 (M2): receive with a timeout so the
                        # resubscribe flag is honored even on a silent book —
                        # `async for raw in ws` only woke on a message, and a
                        # bench promotion could wait hours for its subscription.
                        # Heartbeat (10s) keeps the socket alive between ticks.
                        # Round 2 (adversarial): process the message we already
                        # pulled off the socket BEFORE honoring a resubscribe —
                        # the old order dropped an in-flight fill on the floor.
                        try:
                            raw = await asyncio.wait_for(
                                ws.receive(), timeout=self._ws_recv_timeout
                            )
                        except asyncio.TimeoutError:
                            if self._resubscribe:
                                self._resubscribe = False
                                log.info("resubscribing websocket with updated market set")
                                raise _ResubscribeRequested()
                            continue  # quiet book: keep waiting
                        if raw.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            raise ConnectionResetError(f"ws closed: {raw.type}")
                        if raw.type != aiohttp.WSMsgType.TEXT:
                            if self._resubscribe:
                                self._resubscribe = False
                                log.info("resubscribing websocket with updated market set")
                                raise _ResubscribeRequested()
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
                                _dispatch(on_book_top, book.top())
                        elif mtype == "orderbook_delta":
                            m = msg["msg"]
                            book = books.get(m["market_ticker"])
                            if book:
                                book.apply_delta(m)
                                _dispatch(on_book_top, book.top())
                        elif mtype == "fill":
                            m = msg["msg"]
                            count = int(Decimal(str(m.get("count_fp") or m.get("count") or 0)))
                            yp = _dec(m.get("yes_price_dollars"))
                            if yp is None and m.get("yes_price") is not None:
                                yp = Decimal(m["yes_price"]) / 100
                            # 2026-07-17 (M7): prefer the exchange's own
                            # fee_cost (fixed-point dollars, per the asyncapi
                            # fill schema); fall back to the configured formula
                            # and say so — net-of-fee accounting must know
                            # which number it's holding.
                            reported = _dec(m.get("fee_cost"))
                            if reported is not None:
                                # 2026-07-18 (round 2) guardrail: fee_cost is
                                # documented in fixed-point DOLLARS. A taker fee
                                # can never exceed count*price (the whole cost),
                                # so a value above that means the field changed
                                # units (e.g. integer cents) — refuse it and
                                # fall through to the formula rather than
                                # 100x-booking a fee into the kill switch.
                                fee_cap = count * (yp or Decimal(1))
                                if fee_cap > 0 and reported > fee_cap:
                                    log.error(
                                        "fee_cost %s implausible vs cap %s on %s — "
                                        "using formula (check API units)",
                                        reported, fee_cap, m.get("market_ticker"),
                                    )
                                    reported = None
                            if reported is not None:
                                fee, fee_source = reported, "reported"
                            elif self.fee_schedule is not None:
                                fee = compute_fee(
                                    self.fee_schedule, count,
                                    yp or Decimal(0), bool(m.get("is_taker")),
                                )
                                fee_source = "computed"
                            else:
                                fee, fee_source = Decimal(0), "none"
                            _dispatch(
                                on_fill,
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
                                    fee=fee,
                                    fee_source=fee_source,
                                    raw=m,
                                ),
                            )
                        elif mtype == "error":
                            log.error("ws error message: %s", msg)
                        if self._resubscribe:
                            self._resubscribe = False
                            log.info("resubscribing websocket with updated market set")
                            raise _ResubscribeRequested()
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
    # "bmm-" tags our orders so the reconcile loop can distinguish bot orders
    # from anything the owner places by hand in the Kalshi UI (Round 2: the
    # orphan-cancel path only ever touches bmm- tagged orders).
    return f"bmm-{uuid.uuid4()}"
