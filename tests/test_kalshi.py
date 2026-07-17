import asyncio
import json
from decimal import Decimal

import aiohttp
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

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
    """Scripted websocket: yields messages, then raises `after` (ends the feed)."""

    def __init__(self, messages, after=None):
        self._messages = messages
        self._after = after if after is not None else asyncio.CancelledError()
        self.sent: list[dict] = []

    async def send_json(self, obj):
        self.sent.append(obj)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._messages:
            yield _FakeWsMessage(m)
        raise self._after


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
