import asyncio
import json
from decimal import Decimal

import aiohttp
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from bacchus_mm.exchange.base import Side
from bacchus_mm.exchange.kalshi import (
    KalshiAuth,
    KalshiExchange,
    OrderBook,
    _fmt_count,
    _fmt_price,
    fill_signed_count,
)


def test_auth_signature_verifies():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    auth = KalshiAuth("key-id", pem)
    headers = auth.headers("GET", "/trade-api/v2/portfolio/balance")
    assert headers["KALSHI-ACCESS-KEY"] == "key-id"
    message = (
        headers["KALSHI-ACCESS-TIMESTAMP"] + "GET" + "/trade-api/v2/portfolio/balance"
    ).encode()
    import base64

    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )  # raises on mismatch


def test_inline_private_key_with_escaped_newlines(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    from bacchus_mm.config import Credentials

    creds = Credentials(
        key_id="k",
        private_key_path=None,
        private_key_inline=pem.rstrip("\n").replace("\n", "\\n"),
    )
    assert creds.present
    auth = KalshiAuth(creds.key_id, creds.private_key_pem())
    assert auth.headers("GET", "/x")["KALSHI-ACCESS-KEY"] == "k"


def test_inline_key_wins_over_path():
    from bacchus_mm.config import Credentials

    creds = Credentials(key_id="k", private_key_path="/nonexistent", private_key_inline=None)
    assert creds.present  # path variant present even though file check happens later


def test_price_and_count_formatting():
    assert _fmt_price(Decimal("0.56")) == "0.5600"
    assert _fmt_price(Decimal("0.05")) == "0.0500"
    assert _fmt_count(10) == "10.00"


def test_fill_signed_count_all_quadrants():
    assert fill_signed_count("yes", "buy", 5) == 5
    assert fill_signed_count("yes", "sell", 5) == -5
    assert fill_signed_count("no", "buy", 5) == -5
    assert fill_signed_count("no", "sell", 5) == 5


def test_orderbook_snapshot_delta_and_top():
    book = OrderBook("MKT")
    book.apply_snapshot(
        {
            "yes_dollars_fp": [["0.4400", "100.00"], ["0.4500", "50.00"]],
            "no_dollars_fp": [["0.5200", "80.00"], ["0.5300", "40.00"]],
            "ts_ms": 1,
        }
    )
    top = book.top()
    assert top.bid == Decimal("0.4500")
    assert top.ask == Decimal("1") - Decimal("0.5300")  # 0.47
    assert top.mid == Decimal("0.46")

    # Remove the best yes bid entirely.
    book.apply_delta({"side": "yes", "price_dollars": "0.4500", "delta_fp": "-50.00", "ts_ms": 2})
    assert book.top().bid == Decimal("0.4400")

    # Add size at a better no price -> tightens the ask.
    book.apply_delta({"side": "no", "price_dollars": "0.5400", "delta_fp": "25.00", "ts_ms": 3})
    assert book.top().ask == Decimal("0.4600")


def test_orderbook_empty_side_gives_none():
    book = OrderBook("MKT")
    book.apply_snapshot({"yes_dollars_fp": [["0.4400", "10.00"]], "no_dollars_fp": [], "ts_ms": 1})
    top = book.top()
    assert top.bid == Decimal("0.4400")
    assert top.ask is None and top.mid is None


# ------------------------------------------------------------ REST helpers

@pytest.mark.asyncio
async def test_get_resting_orders_ticker_filter():
    ex = KalshiExchange(env="demo", auth=None)
    calls = []

    async def fake_request(method, path, *, params=None, body=None, authed=True, retries=3):
        calls.append((path, dict(params or {})))
        return {"orders": []}

    ex._request = fake_request
    assert await ex.get_resting_orders() == []
    assert "ticker" not in calls[0][1]
    await ex.get_resting_orders("MKT-X")
    assert calls[1][0] == "/portfolio/orders"
    assert calls[1][1]["ticker"] == "MKT-X"


# ------------------------------------ stream callback isolation (2026-07-17, H4)

class _FakeWsMessage:
    def __init__(self, payload: dict):
        self.type = aiohttp.WSMsgType.TEXT
        self.data = json.dumps(payload)


class _FakeWs:
    """Scripted websocket: hands out messages via receive(), then raises
    `after` (ends the feed). 2026-07-17 (M2): the stream loop now calls
    ws.receive() under a timeout instead of `async for`."""

    def __init__(self, messages, after=None):
        self._messages = list(messages)
        self._after = after if after is not None else asyncio.CancelledError()
        self.sent: list[dict] = []

    async def send_json(self, obj):
        self.sent.append(obj)

    async def receive(self):
        if self._messages:
            return _FakeWsMessage(self._messages.pop(0))
        raise self._after


class _SilentWs(_FakeWs):
    """Quiet-book websocket (2026-07-17, M2): receive() never returns — the
    only escapes are the caller's timeout or cancellation."""

    async def receive(self):
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class _FakeWsConn:
    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc):
        return False


class _FakeHttpSession:
    """Hands out scripted websockets in connect order; counts connects."""

    def __init__(self, *scripts):
        self._scripts = list(scripts)
        self.connects = 0

    def ws_connect(self, url, headers=None, heartbeat=None):
        self.connects += 1
        messages, after = self._scripts.pop(0)
        return _FakeWsConn(_FakeWs(messages, after))


def _stub_auth():
    class _Auth:
        def headers(self, method, path):
            return {}

    return _Auth()


def _stream_exchange(session: _FakeHttpSession) -> KalshiExchange:
    ex = KalshiExchange(env="demo", auth=_stub_auth())

    async def fake_http():
        return session

    ex._http = fake_http
    return ex


def _fill_msg(trade_id: str, seq: int) -> dict:
    return {
        "type": "fill", "sid": 1, "seq": seq,
        "msg": {
            "trade_id": trade_id, "order_id": "o1", "market_ticker": "MKT",
            "side": "yes", "action": "buy", "count_fp": "5.00",
            "yes_price_dollars": "0.4800", "is_taker": False, "ts_ms": 1,
        },
    }


@pytest.mark.asyncio
async def test_stream_callback_exception_does_not_reconnect(caplog):
    # A bug in a stream callback must be logged loudly and the connection
    # kept — not misreported as a ws disconnect with reconnect/resync.
    session = _FakeHttpSession(([_fill_msg("t1", 1), _fill_msg("t2", 2)], None))
    ex = _stream_exchange(session)
    seen = []

    def bad_on_fill(f):
        seen.append(f.trade_id)
        raise ValueError("callback bug")

    with caplog.at_level("ERROR"), pytest.raises(asyncio.CancelledError):
        async for _ in ex.stream(lambda: ["MKT"], lambda top: None, bad_on_fill):
            pass
    assert seen == ["t1", "t2"]  # both messages processed despite the callback bug
    assert session.connects == 1  # and no reconnect was triggered
    assert any("callback bug" in r.getMessage() for r in caplog.records)
    await ex.close()


@pytest.mark.asyncio
async def test_stream_transport_error_reconnects():
    session = _FakeHttpSession(
        ([_fill_msg("t1", 1)], ConnectionError("socket died")),
        ([], None),  # second connect: end the feed immediately
    )
    ex = _stream_exchange(session)
    seen = []

    with pytest.raises(asyncio.CancelledError):
        async for _ in ex.stream(lambda: ["MKT"], lambda top: None, seen.append):
            pass
    assert [f.trade_id for f in seen] == ["t1"]
    assert session.connects == 2  # transport failure DID reconnect
    await ex.close()


# ------------------------------------------- M2: resubscribe starvation fix

@pytest.mark.asyncio
async def test_resubscribe_honored_during_silence_within_timeout():
    """2026-07-17 (M2): a bench promotion requests resubscribe while the book
    is dead quiet. The old `async for` only checked the flag on message
    arrival, so the new ticker could stay unsubscribed for hours; the receive
    timeout must surface it within ws_recv_timeout_seconds."""
    session = _FakeHttpSession(
        ([], None),  # first connect: silent forever
        ([], None),  # second connect (post-resubscribe): ends the feed
    )
    ex = KalshiExchange(env="demo", auth=_stub_auth(), ws_recv_timeout_seconds=0.05)

    async def fake_http():
        return session

    ex._http = fake_http
    # Patch the session to hand out silent sockets (counting connects).
    def silent_connect(url, headers=None, heartbeat=None):
        session.connects += 1
        return _FakeWsConn(_SilentWs([]))

    session.ws_connect = silent_connect

    tickers = [["MKT-A"]]

    async def consume():
        async for _ in ex.stream(lambda: tickers[0], lambda top: None, lambda f: None):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.12)  # a couple of silent receive timeouts pass
    assert session.connects == 1  # still on the first socket: no reason to reconnect
    tickers[0] = ["MKT-A", "MKT-B"]  # bench promotion
    ex.request_resubscribe()
    await asyncio.sleep(0.2)  # must reconnect within ~the 50ms timeout, not hours
    assert session.connects == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await ex.close()


@pytest.mark.asyncio
async def test_silence_without_resubscribe_keeps_socket():
    """The timeout is a flag check, not a reconnect trigger: a quiet book
    with no pending resubscribe stays on one socket."""
    session = _FakeHttpSession(([], None), ([], None))
    ex = KalshiExchange(env="demo", auth=_stub_auth(), ws_recv_timeout_seconds=0.05)

    async def fake_http():
        return session

    ex._http = fake_http

    def silent_connect(url, headers=None, heartbeat=None):
        session.connects += 1
        return _FakeWsConn(_SilentWs([]))

    session.ws_connect = silent_connect

    async def consume():
        async for _ in ex.stream(lambda: ["MKT"], lambda top: None, lambda f: None):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.22)  # several timeouts
    assert session.connects == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await ex.close()


# ------------------------------------------- M6: create-order is non-retrying

@pytest.mark.asyncio
async def test_create_order_never_retries_ambiguous_failures():
    """2026-07-17 (M6): Kalshi's OpenAPI spec documents idempotency only for
    transfers (client_transfer_id), not orders — duplicate client_order_id
    rejection is undocumented, so a retried POST could double-place. Exactly
    one attempt per create_order call, whatever the failure flavor."""
    import aiohttp as _aiohttp

    for failure in (
        _aiohttp.ClientError("conn reset"),
        RuntimeError("POST /portfolio/events/orders -> 500: boom"),
    ):
        ex = KalshiExchange(env="demo", auth=_stub_auth())
        attempts = 0

        async def fake_request(method, path, *, params=None, body=None, authed=True, retries=3):
            nonlocal attempts
            attempts += 1
            assert retries == 0  # the adapter asked for no retries
            raise failure

        ex._request = fake_request
        with pytest.raises(Exception):
            await ex.create_order(
                ticker="MKT", side=Side.BID,
                price=Decimal("0.40"), count=1, client_order_id="c1",
            )
        assert attempts == 1
        await ex.close()


@pytest.mark.asyncio
async def test_create_order_post_only_default_and_override():
    """2026-07-17 (M4): post_only stays True for every normal caller; the
    wind-down cross_1tick escalation is the only post_only=False path."""
    bodies = []
    ex = KalshiExchange(env="demo", auth=_stub_auth())

    async def fake_request(method, path, *, params=None, body=None, authed=True, retries=3):
        bodies.append(body)
        return {"order": {"order_id": "o1", "status": "resting"}}

    ex._request = fake_request
    await ex.create_order(ticker="MKT", side=Side.BID, price=Decimal("0.40"),
                          count=1, client_order_id="c1")
    await ex.create_order(ticker="MKT", side=Side.ASK, price=Decimal("0.42"),
                          count=1, client_order_id="c2", post_only=False)
    assert bodies[0]["post_only"] is True
    assert bodies[1]["post_only"] is False
    await ex.close()
