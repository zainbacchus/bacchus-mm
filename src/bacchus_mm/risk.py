"""Risk management: position caps, PnL tracking, and the kill switch.

PnL accounting is in yes-equivalent terms (buying NO at p is identical to
selling YES at 1-p, so a single signed position per market is sufficient):

    equity_delta = cash + sum(position * mid) - starting value of positions

The kill switch triggers on drawdown from the account-equity high-water mark,
so a run that gets up $100 and gives back the threshold halts too — giving back
profits is the same information as losing from flat. 2026-07-17 (FIX-PnL): the
high-water mark and PnL chain across sessions via cumulative_offset — per-session
rebasing understated true losses 2.9x across the first 8 live sessions.

A halt writes a HALTED marker file; the bot refuses to start while it exists
(`bacchus-mm halt-clear` removes it after you've reviewed the incident).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional


@dataclass
class RiskParams:
    max_contracts_per_market: int = 20
    max_notional_per_market: Decimal = Decimal("100")
    max_gross_notional: Decimal = Decimal("600")
    kill_switch_drawdown: Decimal = Decimal("250")


@dataclass
class MarketState:
    position: int = 0  # signed yes-equivalent contracts
    cash: Decimal = Decimal(0)  # cumulative signed cashflow from fills
    last_mid: Optional[Decimal] = None
    fills: int = 0
    unvalued_seed: int = 0  # seeded contracts awaiting their first mid for a cost basis


@dataclass
class RiskManager:
    params: RiskParams
    state_dir: Path
    markets: dict[str, MarketState] = field(default_factory=dict)
    # 2026-07-17 (FIX-PnL): cumulative_offset carries prior sessions' PnL into
    # this one (loaded by the caller from the event-log kv store); high_water is
    # the account-equity high-water mark the kill switch measures drawdown from.
    cumulative_offset: Decimal = Decimal("0")
    high_water: Optional[Decimal] = None
    halted: bool = False
    halt_reason: Optional[str] = None
    # Set when drawdown() pushes high_water above its load-time value, so the
    # caller knows it should persist the new mark.
    new_high_water_since_load: bool = False
    # 2026-07-17 (C3): resting-order exposure per (ticker, side). Caps must see
    # the true worst case — position plus ALL resting same-direction orders
    # filling — not just settled position (review: phantom overhang ~7-10%
    # beyond stated caps). Registered on placement, released on fill/cancel/
    # TTL-drop; the Pass-2 reconcile resyncs any missed release, so the
    # release paths stay simple rather than exhaustive.
    resting: dict[tuple[str, str], int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.high_water is None:
            # 2026-07-17 (FIX-PnL): the first run after this upgrade has no
            # persisted high-water. Anchor it at the loaded offset so losses
            # from before cumulative accounting existed don't instantly trip
            # the kill switch — they were never measured against it.
            self.high_water = self.cumulative_offset

    @property
    def halt_file(self) -> Path:
        return self.state_dir / "HALTED"

    def check_halt_file(self) -> Optional[str]:
        if self.halt_file.exists():
            return self.halt_file.read_text().strip()
        return None

    def seed_position(self, ticker: str, position: int, mid: Optional[Decimal]) -> None:
        st = self.markets.setdefault(ticker, MarketState())
        st.position = position
        # Value pre-existing inventory at first sight so session PnL starts at 0.
        if mid is not None:
            st.cash = -position * mid
            st.last_mid = mid
        else:
            st.unvalued_seed = position

    def on_fill(self, ticker: str, signed_count: int, yes_price: Decimal) -> None:
        st = self.markets.setdefault(ticker, MarketState())
        st.position += signed_count
        st.cash -= signed_count * yes_price  # buy costs cash, sell raises it
        st.fills += 1

    def on_mid(self, ticker: str, mid: Decimal) -> None:
        st = self.markets.setdefault(ticker, MarketState())
        if st.unvalued_seed:
            # First mark for a seeded position that had no market mid at seed time:
            # value the seeded contracts here so session PnL starts from zero.
            # Fills that arrived in between already set their own basis via cash.
            st.cash -= st.unvalued_seed * mid
            st.unvalued_seed = 0
        st.last_mid = mid

    def pnl(self) -> Decimal:
        total = Decimal(0)
        for st in self.markets.values():
            mark = st.last_mid if st.last_mid is not None else Decimal("0.5")
            # Seeded contracts with no basis yet contribute zero until first mid.
            total += st.cash + (st.position - st.unvalued_seed) * mark
        return total

    @property
    def cumulative_pnl(self) -> Decimal:
        """Account-level PnL: prior sessions (offset) + this session."""
        return self.cumulative_offset + self.pnl()

    def gross_contracts(self) -> int:
        return sum(abs(st.position) for st in self.markets.values())

    def drawdown(self) -> Decimal:
        c = self.cumulative_pnl
        if c > self.high_water:
            self.high_water = c
            self.new_high_water_since_load = True
        return self.high_water - c

    def should_halt(self) -> Optional[str]:
        dd = self.drawdown()
        if dd >= self.params.kill_switch_drawdown:
            return (
                f"kill switch: cumulative drawdown ${dd:.2f} >= "
                f"${self.params.kill_switch_drawdown:.2f} "
                f"(cumulative pnl ${self.cumulative_pnl:.2f}, high water ${self.high_water:.2f})"
            )
        return None

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.halt_file.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {reason}\n")

    def clear_halt(self) -> bool:
        if self.halt_file.exists():
            self.halt_file.unlink()
            return True
        return False

    # ------------------------------------------------------------ order gate

    @staticmethod
    def _resting_key(ticker: str, side: str) -> tuple[str, str]:
        # Accepts a plain "bid"/"ask" string or a Side enum (str-valued); the
        # registry stays exchange-agnostic either way.
        return (ticker, getattr(side, "value", side))

    def register_order(self, ticker: str, side: str, count: int) -> None:
        key = self._resting_key(ticker, side)
        self.resting[key] = self.resting.get(key, 0) + count

    def release_order(self, ticker: str, side: str, count: int) -> None:
        # Idempotent-safe: a double release (cancel racing a fill, TTL-expiry
        # after a cancel) clamps at 0 rather than driving the count negative.
        # Silent on purpose — the Pass-2 reconcile resyncs any drift.
        key = self._resting_key(ticker, side)
        self.resting[key] = max(0, self.resting.get(key, 0) - count)

    def approve_order(
        self, ticker: str, signed_count: int, price: Decimal
    ) -> tuple[bool, str]:
        """Gate for a would-be new order. signed_count: + for bid, - for ask."""
        if self.halted:
            return False, "halted"
        st = self.markets.setdefault(ticker, MarketState())
        after = st.position + signed_count
        # 2026-07-17 (C2): a risk-reducing order is ALWAYS approvable (halt
        # aside) — rejecting exits while over cap traps inventory; hit live
        # 7/15->16 when the cap dropped 20->10 mid-session while holding 13.
        # This precedes every cap check, including the resting-exposure math.
        if abs(after) < abs(st.position):
            return True, "risk reduction"
        # 2026-07-17 (C3): the cap sees the true worst case — position plus
        # every resting same-direction order plus this one all filling.
        if signed_count > 0:
            worst = st.position + self.resting.get((ticker, "bid"), 0) + signed_count
        else:
            worst = st.position - self.resting.get((ticker, "ask"), 0) + signed_count
        if abs(worst) > self.params.max_contracts_per_market:
            return False, (
                f"per-market contract cap: worst case |{worst}| > "
                f"{self.params.max_contracts_per_market}"
            )
        mark = st.last_mid if st.last_mid is not None else price
        if abs(after) * mark > self.params.max_notional_per_market:
            return False, f"per-market notional cap ${self.params.max_notional_per_market}"
        gross_after = self.gross_contracts() - abs(st.position) + abs(after)
        gross_notional = gross_after * Decimal("1.0")  # worst case: every contract settles at $1
        if gross_notional > self.params.max_gross_notional:
            return False, f"gross notional cap ${self.params.max_gross_notional}"
        return True, "ok"
