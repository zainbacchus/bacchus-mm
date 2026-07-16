from decimal import Decimal

from bacchus_mm.strategy.avellaneda_stoikov import (
    StrategyParams,
    VolEstimator,
    compute_quotes,
)

P = StrategyParams()


def quotes(mid="0.50", inv=0, max_inv=20, sigma=0.005, p=P):
    return compute_quotes(Decimal(mid), inv, max_inv, sigma, p)


def test_flat_inventory_quotes_straddle_mid():
    q = quotes()
    assert q.bid is not None and q.ask is not None
    assert q.bid < Decimal("0.50") < q.ask
    assert q.bid_size == P.quote_size and q.ask_size == P.quote_size


def test_long_inventory_skews_quotes_down():
    flat = quotes(inv=0)
    long = quotes(inv=15)
    assert long.reservation < flat.reservation
    # Long yes: our ask should get more aggressive (lower) to shed inventory.
    assert long.ask <= flat.ask
    # And we buy less.
    assert long.bid_size < flat.bid_size


def test_short_inventory_skews_quotes_up():
    flat = quotes(inv=0)
    short = quotes(inv=-15)
    assert short.reservation > flat.reservation
    assert short.bid >= flat.bid
    assert short.ask_size < flat.ask_size


def test_at_cap_stops_growing_side():
    q = quotes(inv=20, max_inv=20)
    assert q.bid is None and q.bid_size == 0  # can't get longer
    assert q.ask is not None and q.ask_size > 0  # can reduce


def test_price_band_suppresses_tail_quotes():
    q = quotes(mid="0.06")
    assert q.bid is None  # below min_price band
    q2 = quotes(mid="0.95")
    assert q2.ask is None


def test_quotes_never_cross_mid():
    for mid in ("0.15", "0.50", "0.85"):
        for inv in (-20, -10, 0, 10, 20):
            q = quotes(mid=mid, inv=inv)
            if q.bid is not None:
                assert q.bid < Decimal(mid)
            if q.ask is not None:
                assert q.ask > Decimal(mid)


def test_high_sigma_widens_spread():
    calm = quotes(sigma=0.004)
    wild = quotes(sigma=0.05)
    assert wild.half_spread >= calm.half_spread


def test_half_spread_clamped():
    q = quotes(sigma=10.0)
    assert q.half_spread == P.max_half_spread


def test_vol_estimator_floors_and_updates():
    v = VolEstimator(halflife_seconds=60, floor=0.004)
    assert v.sigma == 0.004
    t = 1000.0
    v.update(Decimal("0.50"), t)
    for i in range(1, 50):
        v.update(Decimal("0.50") + Decimal("0.02") * (1 if i % 2 else -1), t + i)
    assert v.sigma > 0.004


def test_join_best_joins_when_edge_remains():
    from bacchus_mm.strategy.avellaneda_stoikov import apply_join_best
    q = quotes(mid="0.50")  # bid ~0.47, ask ~0.53, reservation 0.50
    out = apply_join_best(q, Decimal("0.48"), Decimal("0.52"))
    assert out.bid == Decimal("0.48") and out.joined_bid  # joined best bid, 2c edge kept
    assert out.ask == Decimal("0.52") and out.joined_ask


def test_join_best_refuses_without_edge_or_tight_book():
    from bacchus_mm.strategy.avellaneda_stoikov import apply_join_best
    q = quotes(mid="0.50")
    # book best bid at 0.49: joining leaves only 1c edge vs reservation -> refuse
    out = apply_join_best(q, Decimal("0.49"), Decimal("0.53"))
    assert not out.joined_bid and out.bid < Decimal("0.49")
    # tight book (2c spread) -> never join
    q2 = quotes(mid="0.50")
    out2 = apply_join_best(q2, Decimal("0.49"), Decimal("0.51"))
    assert not out2.joined_bid and not out2.joined_ask
