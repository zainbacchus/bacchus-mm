"""Regression (review round 2): a Polymarket outage at startup must NOT kill
the live bot — bad pairs are skipped and the recorder returns cleanly, which
H3 supervision treats as benign."""

import asyncio

import pytest

from bacchus_mm import crossvenue
from bacchus_mm.crossvenue import VenuePair, run_recorder
from bacchus_mm.eventlog import EventLog
from bacchus_mm.main import supervise


class _DownPolymarket:
    """Gamma API unreachable at bot startup (DNS blip / API outage)."""

    async def get_market(self, slug):
        raise ConnectionError("gamma-api.polymarket.com unreachable")

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_polymarket_outage_at_startup_does_not_stop_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(crossvenue, "PolymarketData", _DownPolymarket)
    events = EventLog(tmp_path, "t")
    stop_event = asyncio.Event()
    pairs = [VenuePair(kalshi_ticker="KX-A", polymarket_slug="some-slug")]

    # Exactly what main.cmd_trade does: _spawn(run_recorder(...), "crossvenue")
    t = asyncio.create_task(run_recorder(pairs, None, events, 15.0))
    supervise(t, "crossvenue", stop_event, events)
    await asyncio.sleep(0.05)

    # The analytics sidecar skips the unresolvable pair and returns cleanly:
    # the trading bot keeps running.
    assert t.done() and t.exception() is None
    assert not stop_event.is_set(), "sidecar outage must not fail-stop the bot"
    row = events.db.execute(
        "SELECT payload FROM events WHERE type='task_died'"
    ).fetchone()
    assert row is None
    events.close()
