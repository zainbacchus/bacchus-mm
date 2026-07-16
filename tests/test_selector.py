from decimal import Decimal

from bacchus_mm.exchange.base import MarketInfo
from bacchus_mm.selector import SelectorParams, select_markets


def mk(ticker, bid="0.40", ask="0.46", vol=1000, prev=None, category="Economics",
       close="2099-01-01T00:00:00Z", event=None):
    raw = {}
    if prev is not None:
        raw["previous_price_dollars"] = prev
    return MarketInfo(
        ticker=ticker, event_ticker=event or ticker + "-EV", title=ticker,
        category=category, close_time=close,
        yes_bid=Decimal(bid), yes_ask=Decimal(ask),
        volume_24h=Decimal(vol), open_interest=Decimal(0), raw=raw,
    )


def test_trend_filter_excludes_falling_knives():
    p = SelectorParams()
    calm = mk("CALM", prev="0.44")          # mid 0.43 vs prev 0.44: fine
    knife = mk("KNIFE", prev="0.60")        # mid 0.43 vs prev 0.60: 17c slide
    unknown = mk("NOPREV")                  # no prev data: allowed through
    picks = {s.market.ticker for s in select_markets([calm, knife, unknown], p)}
    assert "CALM" in picks and "NOPREV" in picks
    assert "KNIFE" not in picks


def test_one_market_per_event_and_top_n():
    p = SelectorParams(max_markets=2)
    a1 = mk("A1", vol=5000, event="EV-A")
    a2 = mk("A2", vol=4000, event="EV-A")
    b = mk("B", vol=3000, event="EV-B")
    c = mk("C", vol=100, event="EV-C")  # below min_volume_24h
    picks = [s.market.ticker for s in select_markets([a1, a2, b, c], p)]
    assert len(picks) == 2
    assert "A2" not in picks  # same event as A1
    assert "C" not in picks
