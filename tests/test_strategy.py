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
    q = quotes(mid="0.50")  # bid ~0.46, ask ~0.54, reservation 0.50
    out = apply_join_best(q, Decimal("0.48"), Decimal("0.52"))
    assert out.bid == Decimal("0.48") and out.joined_bid  # joined best bid, 2c edge kept
    assert out.ask == Decimal("0.52") and out.joined_ask


# 2026-07-17 (H1 policy A): join fires on any book >= 2c with >= 1c of
# reservation edge — 2.7% join rate and 0.26% fill rate said the old band was
# inert. Gate: revert if markout@+600s < -0.5c/contract over >= 60 fills.
def test_join_best_policy_a_fires_at_2c_3c_5c_books():
    from bacchus_mm.strategy.avellaneda_stoikov import apply_join_best
    for bid, ask in (("0.49", "0.51"), ("0.49", "0.52"), ("0.48", "0.53")):
        q = quotes(mid="0.50")
        out = apply_join_best(q, Decimal(bid), Decimal(ask))
        assert out.joined_bid and out.bid == Decimal(bid), (bid, ask)
        assert out.joined_ask and out.ask == Decimal(ask), (bid, ask)


def test_join_best_never_joins_sub_2c_book():
    from bacchus_mm.strategy.avellaneda_stoikov import apply_join_best
    q = quotes(mid="0.50")
    out = apply_join_best(q, Decimal("0.495"), Decimal("0.505"))  # 1c book
    assert not out.joined_bid and not out.joined_ask
    assert out.bid < Decimal("0.495") and out.ask > Decimal("0.505")


def test_join_best_never_crosses_the_touch():
    from bacchus_mm.strategy.avellaneda_stoikov import apply_join_best
    # Model bid already AT/ABOVE best bid: join must not push past the touch.
    q = quotes(mid="0.50")
    out = apply_join_best(q, Decimal("0.45"), Decimal("0.47"))  # model bid 0.46 > 0.45
    assert out.bid == Decimal("0.46") and not out.joined_bid
    # Joined quotes sit exactly on the touch, never through it.
    for bid, ask in (("0.49", "0.51"), ("0.48", "0.53")):
        out = apply_join_best(quotes(mid="0.50"), Decimal(bid), Decimal(ask))
        assert out.bid <= Decimal(bid) and out.ask >= Decimal(ask)


def test_join_best_refuses_when_reservation_edge_below_margin():
    from bacchus_mm.strategy.avellaneda_stoikov import apply_join_best
    # inv=2 pulls reservation to ~0.446; book bid 0.44 leaves only 0.6c edge.
    q = quotes(mid="0.50", inv=2)
    out = apply_join_best(q, Decimal("0.44"), Decimal("0.47"))
    assert not out.joined_bid and out.bid < Decimal("0.44")
    # The other side still has >= 1c edge, so it joins.
    assert out.joined_ask and out.ask == Decimal("0.47")
