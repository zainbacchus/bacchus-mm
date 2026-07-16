"""Configuration: config.yaml defaults, config.local.yaml overlay, secrets from env.

config.yaml is committed (public defaults); config.local.yaml is gitignored and
holds your tuned parameters. Credentials only ever come from the environment
(or a .env file loaded before launch) — never from config files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import yaml

from .risk import RiskParams
from .selector import SelectorParams
from .strategy.avellaneda_stoikov import StrategyParams


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _dec(section: dict, key: str, default: Decimal) -> Decimal:
    v = section.get(key)
    return Decimal(str(v)) if v is not None else default


@dataclass
class Credentials:
    key_id: Optional[str]
    private_key_path: Optional[str]
    private_key_inline: Optional[str]

    @property
    def present(self) -> bool:
        return bool(self.key_id and (self.private_key_path or self.private_key_inline))

    def private_key_pem(self) -> bytes:
        """Inline key wins over path. Inline values may carry literal \\n escapes
        (the one-line .env convention for multi-line PEMs)."""
        if self.private_key_inline:
            return self.private_key_inline.replace("\\n", "\n").encode()
        with open(self.private_key_path, "rb") as f:
            return f.read()


@dataclass
class Config:
    env: str
    live_enabled: bool
    data_dir: Path
    write_tokens_per_second: float
    order_group_contracts_per_15s: int
    selector: SelectorParams
    strategy: StrategyParams
    risk: RiskParams
    requote_min_interval: float
    requote_tolerance: Decimal
    order_ttl_seconds: int
    fast_move_threshold: Decimal
    fast_move_window: float
    fast_move_cooloff: float
    guard_evict_trips: int
    selector_refresh_minutes: int
    raw: dict = field(repr=False, default_factory=dict)

    @classmethod
    def load(cls, root: Path | str = ".") -> "Config":
        root = Path(root)
        data: dict[str, Any] = {}
        for name in ("config.yaml", "config.local.yaml"):
            path = root / name
            if path.exists():
                with open(path) as f:
                    data = _deep_merge(data, yaml.safe_load(f) or {})

        sel = data.get("selector", {})
        stra = data.get("strategy", {})
        risk = data.get("risk", {})
        exch = data.get("exchange", {})

        selector = SelectorParams(
            categories=sel.get("categories", SelectorParams().categories),
            ticker_blocklist=sel.get("ticker_blocklist", []),
            min_volume_24h=_dec(sel, "min_volume_24h", Decimal(500)),
            min_spread=_dec(sel, "min_spread", Decimal("0.02")),
            max_spread=_dec(sel, "max_spread", Decimal("0.15")),
            min_price=_dec(sel, "min_price", Decimal("0.10")),
            max_price=_dec(sel, "max_price", Decimal("0.90")),
            min_hours_to_close=float(sel.get("min_hours_to_close", 12)),
            max_markets=int(sel.get("max_markets", 6)),
            volume_weight=float(sel.get("volume_weight", 0.35)),
            spread_weight=float(sel.get("spread_weight", 0.65)),
            max_move_24h=_dec(sel, "max_move_24h", Decimal("0.10")),
        )
        strategy = StrategyParams(
            gamma=float(stra.get("gamma", 0.3)),
            k=float(stra.get("k", 50.0)),
            horizon_seconds=float(stra.get("horizon_seconds", 3600)),
            sigma_floor=float(stra.get("sigma_floor", 0.004)),
            sigma_halflife_seconds=float(stra.get("sigma_halflife_seconds", 600)),
            min_half_spread=_dec(stra, "min_half_spread", Decimal("0.01")),
            max_half_spread=_dec(stra, "max_half_spread", Decimal("0.05")),
            quote_size=int(stra.get("quote_size", 5)),
            min_price=selector.min_price,
            max_price=selector.max_price,
            tick=_dec(stra, "tick", Decimal("0.01")),
        )
        risk_params = RiskParams(
            max_contracts_per_market=int(risk.get("max_contracts_per_market", 20)),
            max_notional_per_market=_dec(risk, "max_notional_per_market", Decimal(100)),
            max_gross_notional=_dec(risk, "max_gross_notional", Decimal(600)),
            kill_switch_drawdown=_dec(risk, "kill_switch_drawdown", Decimal(250)),
        )
        return cls(
            env=os.environ.get("KALSHI_ENV", data.get("env", "demo")),
            live_enabled=bool(data.get("live", {}).get("enabled", False)),
            data_dir=root / data.get("logging", {}).get("dir", "data"),
            write_tokens_per_second=float(exch.get("write_tokens_per_second", 50)),
            order_group_contracts_per_15s=int(risk.get("order_group_contracts_per_15s", 40)),
            selector=selector,
            strategy=strategy,
            risk=risk_params,
            requote_min_interval=float(stra.get("requote_min_interval", 1.0)),
            requote_tolerance=_dec(stra, "requote_tolerance", Decimal("0.01")),
            order_ttl_seconds=int(stra.get("order_ttl_seconds", 900)),
            fast_move_threshold=_dec(stra, "fast_move_threshold", Decimal("0.03")),
            fast_move_window=float(stra.get("fast_move_window", 30)),
            fast_move_cooloff=float(stra.get("fast_move_cooloff", 180)),
            guard_evict_trips=int(stra.get("guard_evict_trips", 8)),
            selector_refresh_minutes=int(sel.get("refresh_minutes", 30)),
            raw=data,
        )

    def credentials(self) -> Credentials:
        return Credentials(
            key_id=os.environ.get("KALSHI_API_KEY_ID"),
            private_key_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH"),
            private_key_inline=os.environ.get("KALSHI_PRIVATE_KEY"),
        )
