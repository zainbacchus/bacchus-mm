"""Attack script (review): prove the stream drops an already-received message
when request_resubscribe() is honored, and that nothing redelivers it."""

import asyncio

import pytest

from test_kalshi import (
    _FakeHttpSession,
    _fill_msg,
    _stream_exchange,
)


@pytest.mark.asyncio
async def test_resubscribe_drops_in_flight_fill():
    # First connection delivers two fills; a resubscribe is requested while
    # processing the first (bench promotion timing). Second connection is empty
    # (the fill channel does not replay history per the asyncapi spec).
    session = _FakeHttpSession(
        ([_fill_msg("t1", 1), _fill_msg("t2", 2)], None),
        ([], None),
    )
    ex = _stream_exchange(session)
    seen = []

    def on_fill(f):
        seen.append(f.trade_id)
        if f.trade_id == "t1":
            ex.request_resubscribe()  # what bench_loop does on promotion

    with pytest.raises(asyncio.CancelledError):
        async for _ in ex.stream(lambda: ["MKT"], lambda top: None, on_fill):
            pass

    assert session.connects == 2, "resubscribe should reconnect"
    # THE BUG EVIDENCE: fill t2 was already received from the socket but is
    # dropped by the `if self._resubscribe: raise` check before dispatch.
    assert seen == ["t1"], f"t2 was dropped: saw {seen}"
    await ex.close()
