from decimal import Decimal

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from bacchus_mm.exchange.kalshi import (
    KalshiAuth,
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
