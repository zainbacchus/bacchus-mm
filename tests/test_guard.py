from decimal import Decimal

from bacchus_mm.marketmaker import FastMoveGuard


def g():
    return FastMoveGuard(Decimal("0.03"), window_s=30, cooloff_s=180)


def test_slow_drift_does_not_trip():
    guard = g()
    # 10c of drift, but spread over 10 minutes — never >=3c inside any 30s window
    for i in range(100):
        guard.update(Decimal("0.40") + Decimal("0.001") * i, ts=1000.0 + i * 6)
    assert not guard.blocked(ts=1600.0)


def test_fast_move_trips_and_cools_off():
    guard = g()
    guard.update(Decimal("0.40"), ts=1000.0)
    guard.update(Decimal("0.39"), ts=1010.0)
    guard.update(Decimal("0.36"), ts=1020.0)  # 4c in 20s -> trip
    assert guard.blocked(ts=1021.0)
    assert guard.blocked(ts=1020.0 + 179)
    assert not guard.blocked(ts=1020.0 + 181)


def test_retriggers_extend_cooloff():
    guard = g()
    guard.update(Decimal("0.40"), ts=1000.0)
    guard.update(Decimal("0.30"), ts=1005.0)  # trip -> blocked until 1185
    guard.update(Decimal("0.20"), ts=1100.0)  # still moving -> blocked until 1280
    assert guard.blocked(ts=1200.0)
    assert not guard.blocked(ts=1281.0)


def test_old_history_expires():
    guard = g()
    guard.update(Decimal("0.40"), ts=1000.0)
    guard.update(Decimal("0.40"), ts=1040.0)  # first point aged out of window
    guard.update(Decimal("0.37"), ts=1041.0)  # 3c vs 0.40@1040 -> trip
    assert guard.blocked(ts=1042.0)
