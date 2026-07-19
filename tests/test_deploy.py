"""Deployment-layer tests (2026-07-17, DEPLOY): /health endpoint shape and
failure modes, health/live config plumbing, PEM-from-env credentials, and the
startup clock-skew check (fake HTTP responses with controlled Date headers)."""

import asyncio
import json
import time
from decimal import Decimal
from email.utils import formatdate
from types import SimpleNamespace

import aiohttp
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from bacchus_mm.config import Config
from bacchus_mm.eventlog import EventLog
from bacchus_mm.exchange.kalshi import (
    KalshiAuth,
    rest_clock_skew_seconds,
    skew_from_date_header,
)
from bacchus_mm.health import HealthState, bound_port, start_health_server
from bacchus_mm.main import check_clock_skew
from bacchus_mm.risk import RiskManager, RiskParams

EXPECTED_KEYS = {
    "mode", "live", "halted", "uptime_s", "last_event_age_s",
    "markets_active", "cumulative_pnl", "version",
}


def _state(tmp_path, mode="run", live=False):
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    workers = {"MKT": SimpleNamespace(evicted=False), "OLD": SimpleNamespace(evicted=True)}
    return HealthState(mode=mode, live=live, risk=risk, workers=workers)


async def _get(port, path="/health"):
    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{port}{path}") as resp:
            return resp.status, await resp.text()


# ------------------------------------------------------------- /health shape

@pytest.mark.asyncio
async def test_health_200_with_expected_keys(tmp_path):
    state = _state(tmp_path)
    runner = await start_health_server(state, port=0, host="127.0.0.1")
    try:
        status, text = await _get(bound_port(runner))
        assert status == 200
        payload = json.loads(text)
        assert set(payload) == EXPECTED_KEYS  # whitelist: nothing extra leaks
        assert payload["mode"] == "run" and payload["live"] is False
        assert payload["halted"] is False
        assert payload["markets_active"] == 1  # evicted worker excluded
        assert payload["cumulative_pnl"] == 0.0
        assert payload["uptime_s"] >= 0 and payload["last_event_age_s"] >= 0
        assert payload["version"]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_health_payload_contains_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY_ID", "sentinel-key-id-xyz")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", "sentinel-pem-xyz")
    runner = await start_health_server(_state(tmp_path), port=0, host="127.0.0.1")
    try:
        _, text = await _get(bound_port(runner))
        assert "sentinel" not in text
        assert "kalshi" not in text.lower()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_health_503_when_halt_marker_exists(tmp_path):
    # A HALTED marker from a previous session (risk.halted is False here —
    # the file alone must trip the 503).
    (tmp_path / "HALTED").write_text("2026-07-17 10:00:00 kill switch drawdown\n")
    state = _state(tmp_path)
    runner = await start_health_server(state, port=0, host="127.0.0.1")
    try:
        status, text = await _get(bound_port(runner))
        assert status == 503
        assert json.loads(text)["halted"] is True
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_health_503_when_stale(tmp_path):
    state = _state(tmp_path)
    state._last_event_mono = time.monotonic() - 301  # wedged loop simulation
    runner = await start_health_server(state, port=0, host="127.0.0.1")
    try:
        status, text = await _get(bound_port(runner))
        assert status == 503
        assert json.loads(text)["last_event_age_s"] > 300
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_health_observe_mode_and_pnl(tmp_path):
    risk = RiskManager(params=RiskParams(), state_dir=tmp_path)
    risk.on_fill("MKT", 5, Decimal("0.40"))
    risk.on_mid("MKT", Decimal("0.50"))
    state = HealthState(mode="observe", live=False, risk=risk, workers={})
    runner = await start_health_server(state, port=0, host="127.0.0.1")
    try:
        status, text = await _get(bound_port(runner))
        assert status == 200
        payload = json.loads(text)
        assert payload["mode"] == "observe"
        assert payload["cumulative_pnl"] == 0.5  # 5 x (0.50 - 0.40)
    finally:
        await runner.cleanup()


def test_eventlog_on_event_hook_fires_and_never_breaks_emit(tmp_path):
    calls = []
    events = EventLog(tmp_path, "t", on_event=lambda: calls.append(1))
    events.emit("test_event")
    assert calls
    events.on_event = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    events.emit("test_event2")  # must not raise
    events.close()


# ------------------------------------------------------- config plumbing

def test_health_port_env_auto_enables(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_PORT", "9099")
    cfg = Config.load(tmp_path)
    assert cfg.health_enabled is True
    assert cfg.health_port == 9099


def test_health_config_defaults_off(tmp_path, monkeypatch):
    monkeypatch.delenv("HEALTH_PORT", raising=False)
    cfg = Config.load(tmp_path)
    assert cfg.health_enabled is False
    assert cfg.health_port == 8080


def test_health_enabled_from_yaml(tmp_path, monkeypatch):
    monkeypatch.delenv("HEALTH_PORT", raising=False)
    (tmp_path / "config.yaml").write_text("health:\n  enabled: true\n  port: 9000\n")
    cfg = Config.load(tmp_path)
    assert cfg.health_enabled is True
    assert cfg.health_port == 9000


def test_live_enabled_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv("BACCHUS_LIVE_ENABLED", raising=False)
    assert Config.load(tmp_path).live_enabled is False
    monkeypatch.setenv("BACCHUS_LIVE_ENABLED", "1")
    assert Config.load(tmp_path).live_enabled is True


# ------------------------------------------------------- PEM from env

def _make_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


def test_private_key_from_env_escaped_form(tmp_path, monkeypatch):
    pem = _make_pem()
    monkeypatch.setenv("KALSHI_API_KEY_ID", "kid-1")
    # The one-line .env convention: literal \n escapes instead of newlines.
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", pem.decode().replace("\n", "\\n"))
    creds = Config.load(tmp_path).credentials()
    assert creds.present
    auth = KalshiAuth(creds.key_id, creds.private_key_pem())
    headers = auth.headers("GET", "/trade-api/v2/portfolio/balance")
    assert set(headers) == {
        "KALSHI-ACCESS-KEY", "KALSHI-ACCESS-SIGNATURE", "KALSHI-ACCESS-TIMESTAMP"
    }


def test_private_key_from_env_real_multiline(tmp_path, monkeypatch):
    # `fly secrets set KALSHI_PRIVATE_KEY="$(cat key.pem)"` stores real
    # newlines — the loader must accept that form too.
    pem = _make_pem()
    monkeypatch.setenv("KALSHI_API_KEY_ID", "kid-1")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", pem.decode())
    creds = Config.load(tmp_path).credentials()
    assert creds.private_key_pem() == pem
    KalshiAuth(creds.key_id, creds.private_key_pem())  # parses


# ------------------------------------------------------- clock skew

def test_skew_from_date_header():
    now = int(time.time())  # HTTP Date is whole-second granularity
    # Server 5s behind local -> skew +5; 3s ahead -> skew -3.
    assert skew_from_date_header(formatdate(now - 5, usegmt=True), now=now) == pytest.approx(5, abs=0.2)
    assert skew_from_date_header(formatdate(now + 3, usegmt=True), now=now) == pytest.approx(-3, abs=0.2)


async def _fake_http_date_server(date_header: str):
    """One-shot HTTP server with a controlled Date header (aiohttp's own
    server would overwrite Date, so this is a raw TCP responder)."""

    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        body = b"{}"
        writer.write(
            (
                "HTTP/1.1 404 Not Found\r\n"
                f"Date: {date_header}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.mark.asyncio
async def test_rest_clock_skew_small_and_large():
    now = time.time()
    server, port = await _fake_http_date_server(formatdate(now, usegmt=True))
    async with server:
        skew = await rest_clock_skew_seconds(f"http://127.0.0.1:{port}/trade-api/v2")
    assert abs(skew) < 2.0

    server, port = await _fake_http_date_server(formatdate(now - 10, usegmt=True))
    async with server:
        skew = await rest_clock_skew_seconds(f"http://127.0.0.1:{port}/trade-api/v2")
    assert skew == pytest.approx(10, abs=2.0)


@pytest.mark.asyncio
async def test_check_clock_skew_emits_warning_over_threshold(tmp_path):
    events = EventLog(tmp_path, "t")
    server, port = await _fake_http_date_server(formatdate(time.time() - 10, usegmt=True))
    ex = SimpleNamespace(rest_url=f"http://127.0.0.1:{port}/trade-api/v2")
    async with server:
        skew = await check_clock_skew(ex, events)
    assert skew is not None and skew > 2
    events.flush()
    rows = events.db.execute(
        "SELECT payload FROM events WHERE type='clock_skew_warning'"
    ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0])["skew_s"] > 2
    events.close()


@pytest.mark.asyncio
async def test_check_clock_skew_quiet_under_threshold(tmp_path):
    events = EventLog(tmp_path, "t")
    server, port = await _fake_http_date_server(formatdate(time.time(), usegmt=True))
    ex = SimpleNamespace(rest_url=f"http://127.0.0.1:{port}/trade-api/v2")
    async with server:
        await check_clock_skew(ex, events)
    events.flush()
    n = events.db.execute(
        "SELECT COUNT(*) FROM events WHERE type='clock_skew_warning'"
    ).fetchone()[0]
    assert n == 0
    events.close()


@pytest.mark.asyncio
async def test_check_clock_skew_network_failure_is_advisory(tmp_path):
    events = EventLog(tmp_path, "t")
    # Nothing listening on this port: the check must return None, not raise.
    ex = SimpleNamespace(rest_url="http://127.0.0.1:1/trade-api/v2")
    assert await check_clock_skew(ex, events) is None
    events.close()
