"""Join-the-touch quoting policy for the 15-minute markets (2026-08-06,
Phase D — ROADMAP).

Why no model: the Phase D study (research/ARB-AND-15MIN-STUDY-2026-08-06.md)
scored a spot-driven fair-value model against the Kalshi mid on 3,480
market-minutes of settled KXBTC15M and the MARKET won at every one of the 14
minutes of a window's life. We therefore do not price these markets, we join
them: quote exactly at the best bid/ask THAT OTHERS ARE ALREADY QUOTING and
measure what passive queue membership earns. The measurement is the product.

Rules, in order:
  * JOIN, never lead: the touch is computed EXCLUDING our own resting order.
    If we are alone at a level, the "external touch" is the next level with
    someone else's size — naively joining the raw book top would ratchet our
    own quote down one tick per requote cycle.
  * Never cross: if the external book is crossed or locked (ext_bid >=
    ext_ask), quote nothing this cycle. Post-only would reject the order
    anyway; skipping saves the round trip.
  * Inventory-capped, symmetric otherwise: no skew, no reservation price.
    Above +cap stop bidding, below -cap stop offering; size never grows
    |position| past the cap.

Kalshi book representation: both sides are BID books — yes_bids at p, no_bids
at p'. A resting YES ask at price a is a NO bid at 1-a. All prices Decimal
dollars on the yes side, per the repo convention.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .avellaneda_stoikov import QuotePair

TWO = Decimal(2)


def _best_excluding(
    levels: dict[Decimal, Decimal], my_price: Optional[Decimal], my_count: int
) -> Optional[Decimal]:
    """Best (max) bid level with size beyond our own resting order."""
    best: Optional[Decimal] = None
    for price, qty in levels.items():
        others = qty
        if my_price is not None and price == my_price:
            others = qty - my_count
        if others > 0 and (best is None or price > best):
            best = price
    return best


def join_touch_quotes(
    yes_bids: dict[Decimal, Decimal],
    no_bids: dict[Decimal, Decimal],
    my_bid_price: Optional[Decimal],
    my_bid_count: int,
    my_ask_price: Optional[Decimal],
    my_ask_count: int,
    inventory: int,
    max_inventory: int,
    size: int,
    tilt_threshold: Optional[Decimal] = None,
    max_loss_per_market: Optional[Decimal] = None,
) -> QuotePair:
    """Compute join-the-touch quotes from full book levels.

    my_*_price/count are our currently-resting orders (yes-side prices), used
    only for self-exclusion. Returns a QuotePair so the MarketWorker emit and
    reconcile paths are shared with the A-S policy; sigma/reservation carry
    placeholder values (there is no model here).

    2026-08-06 evidence-based levers (both off when None/0, see ROADMAP):

    tilt_threshold — favorite-longshot tilt. Kalshi's measured FLB (Buergi,
    Deng & Whelan 2026: makers on >=50c contracts earn +2.6% AFTER fees;
    longshots win far less than priced) says the only historically +EV maker
    fill is going LONG the favorite. In the tails we therefore suppress the
    side that would SELL the favorite / BUY the longshot: no ask-join at
    ask >= threshold, no bid-join at bid <= 1-threshold. Pure tilt — the
    suppression also applies to risk-reducing exits (selling a held favorite
    cheap forgoes the same measured edge); inventory stays bounded by the
    caps below.

    max_loss_per_market — price-shaped inventory cap (the BS-for-PM handbook,
    arXiv 2510.15205: cap notional near the boundaries where replication is
    impossible). Worst-case dollar loss per market is capped: a long q at
    price p can lose q*p, a short q can lose q*(1-p), so the per-side
    contract cap is max_loss/p (long) and max_loss/(1-p) (short), never
    exceeding max_inventory. At p=0.50 with $2.50 that is the flat cap 5; at
    p=0.97 longs cap at 2.
    """
    ext_bid = _best_excluding(yes_bids, my_bid_price, my_bid_count)
    # Our yes-side ask at price a rests in the NO book at 1 - a.
    ext_no = _best_excluding(
        no_bids,
        (Decimal(1) - my_ask_price) if my_ask_price is not None else None,
        my_ask_count,
    )
    ext_ask = (Decimal(1) - ext_no) if ext_no is not None else None

    bid: Optional[Decimal] = ext_bid
    ask: Optional[Decimal] = ext_ask
    # Crossed/locked external book: transient ws state or a book we do not
    # understand — quote nothing rather than guess.
    if bid is not None and ask is not None and bid >= ask:
        bid = ask = None

    tilt_bid = tilt_ask = False
    if tilt_threshold:
        if ask is not None and ask >= tilt_threshold:
            ask, tilt_ask = None, True  # would sell the favorite
        if bid is not None and bid <= Decimal(1) - tilt_threshold:
            bid, tilt_bid = None, True  # would buy the longshot

    eff_max_long = max_inventory
    eff_max_short = max_inventory
    if max_loss_per_market:
        if bid is not None and bid > 0:
            eff_max_long = min(max_inventory, int(max_loss_per_market / bid))
        if ask is not None and ask < 1:
            eff_max_short = min(max_inventory, int(max_loss_per_market / (Decimal(1) - ask)))

    bid_size = min(size, eff_max_long - inventory)
    ask_size = min(size, eff_max_short + inventory)
    if bid_size <= 0:
        bid, bid_size = None, 0
    if ask_size <= 0:
        ask, ask_size = None, 0
    if bid is None:
        bid_size = 0
    if ask is None:
        ask_size = 0

    if bid is not None and ask is not None:
        reservation = (bid + ask) / TWO
        half_spread = (ask - bid) / TWO
    else:
        reservation = bid if bid is not None else (ask if ask is not None else Decimal(0))
        half_spread = Decimal(0)
    q = QuotePair(
        bid=bid,
        bid_size=bid_size,
        ask=ask,
        ask_size=ask_size,
        reservation=reservation,
        half_spread=half_spread,
        sigma=0.0,
        joined_bid=bid is not None,
        joined_ask=ask is not None,
    )
    # Attribution telemetry (2026-08-06): which lever suppressed a side. Set
    # as attributes so the shared QuotePair stays unchanged for the A-S path.
    q.tilt_bid = tilt_bid
    q.tilt_ask = tilt_ask
    return q
