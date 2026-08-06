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
) -> QuotePair:
    """Compute join-the-touch quotes from full book levels.

    my_*_price/count are our currently-resting orders (yes-side prices), used
    only for self-exclusion. Returns a QuotePair so the MarketWorker emit and
    reconcile paths are shared with the A-S policy; sigma/reservation carry
    placeholder values (there is no model here).
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

    bid_size = min(size, max_inventory - inventory)
    ask_size = min(size, max_inventory + inventory)
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
    return QuotePair(
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
