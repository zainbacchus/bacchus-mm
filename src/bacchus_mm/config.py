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

from .fees import FeeSchedule
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
    reconcile_seconds: int
    sweep_cooloff_seconds: int
    selector: SelectorParams
    strategy: StrategyParams
    risk: RiskParams
    requote_min_interval: float
    requote_tolerance: Decimal
    order_ttl_seconds: int
    fast_move_threshold: Decimal
    fast_move_window: float
    fast_move_cooloff: float
    fast_move_spread_multiple: Decimal
    fast_move_confirm_updates: int
    guard_evict_trips: int
    selector_refresh_minutes: int
    # 2026-07-17 (M1): batched eventlog writes + events-table retention.
    log_flush_seconds: float = 1.0
    log_flush_batch: int = 500
    log_events_keep_days: int = 14
    # 2026-07-17 (M3): periodic mid/pnl marks, close reaper, settlement poll.
    marks_tick_seconds: float = 60.0
    close_reaper_hours: float = 12.0
    settlement_poll_seconds: float = 900.0
    # 2026-07-17 (M2): ws receive timeout so a resubscribe can't starve on a
    # quiet book.
    ws_recv_timeout_seconds: float = 30.0
    # 2026-07-17 (M5): prod+live refuses to trade without the exchange-side
    # order group unless this escape hatch is explicitly set.
    allow_no_order_group: bool = False
    # 2026-07-17 (M4): wind-down distress alerting (+ owner-gated escalation).
    winddown_alert_minutes: float = 30.0
    winddown_alert_move: Decimal = Decimal("0.05")
    winddown_escalation: str = "none"
    # 2026-07-17 (M7): per-venue fee schedules (kalshi default always present).
    fees: dict[str, FeeSchedule] = field(default_factory=dict)
    # 2026-07-17 (DEPLOY): /health endpoint for fly.io machine checks. The
    # HEALTH_PORT env var (set by fly.toml) force-enables it and wins over
    # health.port — containers get it without touching config files.
    health_enabled: bool = False
    health_port: int = 8080
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
        log_cfg = data.get("logging", {})
        health_cfg = data.get("health", {})

        # 2026-07-17 (M7): per-venue fee schedules; kalshi defaults apply even
        # without a fees: block so the adapter always has a schedule.
        fee_schedules = {
            venue: FeeSchedule(
                taker_rate=_dec(f or {}, "taker_rate", Decimal("0.07")),
                maker_rate=_dec(f or {}, "maker_rate", Decimal("0")),
                formula=(f or {}).get("formula", "kalshi_v1"),
            )
            for venue, f in data.get("fees", {}).items()
        }
        fee_schedules.setdefault("kalshi", FeeSchedule())

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
            join_margin=_dec(stra, "join_margin", Decimal("0.01")),
            min_book_spread=_dec(stra, "min_book_spread", Decimal("0.02")),
        )
        risk_params = RiskParams(
            max_contracts_per_market=int(risk.get("max_contracts_per_market", 20)),
            max_notional_per_market=_dec(risk, "max_notional_per_market", Decimal(100)),
            max_gross_notional=_dec(risk, "max_gross_notional", Decimal(600)),
            kill_switch_drawdown=_dec(risk, "kill_switch_drawdown", Decimal(250)),
        )
        return cls(
            env=os.environ.get("KALSHI_ENV", data.get("env", "demo")),
            # 2026-07-17 (DEPLOY): containers have no config.local.yaml, so the
            # config-file half of the two-key prod gate also comes from an env
            # var there (BACCHUS_LIVE_ENABLED=1) — still two deliberate keys,
            # now env var + --live flag. See docs/deploy.md.
            live_enabled=bool(data.get("live", {}).get("enabled", False))
            or os.environ.get("BACCHUS_LIVE_ENABLED", "").lower() in ("1", "true", "yes"),
            data_dir=root / data.get("logging", {}).get("dir", "data"),
            write_tokens_per_second=float(exch.get("write_tokens_per_second", 50)),
            order_group_contracts_per_15s=int(risk.get("order_group_contracts_per_15s", 40)),
            # 2026-07-17 (C1): resting-order reconcile cadence + sweep cooloff
            reconcile_seconds=int(exch.get("reconcile_seconds", 45)),
            sweep_cooloff_seconds=int(exch.get("sweep_cooloff_seconds", 900)),
            selector=selector,
            strategy=strategy,
            risk=risk_params,
            requote_min_interval=float(stra.get("requote_min_interval", 1.0)),
            requote_tolerance=_dec(stra, "requote_tolerance", Decimal("0.01")),
            order_ttl_seconds=int(stra.get("order_ttl_seconds", 900)),
            fast_move_threshold=_dec(stra, "fast_move_threshold", Decimal("0.03")),
            fast_move_window=float(stra.get("fast_move_window", 30)),
            fast_move_cooloff=float(stra.get("fast_move_cooloff", 180)),
            fast_move_spread_multiple=_dec(stra, "fast_move_spread_multiple", Decimal("0.75")),
            fast_move_confirm_updates=int(stra.get("fast_move_confirm_updates", 2)),
            guard_evict_trips=int(stra.get("guard_evict_trips", 8)),
            selector_refresh_minutes=int(sel.get("refresh_minutes", 30)),
            log_flush_seconds=float(log_cfg.get("flush_seconds", 1.0)),
            log_flush_batch=int(log_cfg.get("flush_batch", 500)),
            log_events_keep_days=int(log_cfg.get("events_keep_days", 14)),
            # 2026-07-17 (M3/M2): ops cadences live under exchange:.
            marks_tick_seconds=float(exch.get("marks_tick_seconds", 60)),
            close_reaper_hours=float(exch.get("close_reaper_hours", 12)),
            settlement_poll_seconds=float(exch.get("settlement_poll_seconds", 900)),
            ws_recv_timeout_seconds=float(exch.get("ws_recv_timeout_seconds", 30)),
            # 2026-07-17 (M5): fail-closed order-group requirement (risk:).
            allow_no_order_group=bool(risk.get("allow_no_order_group", False)),
            # 2026-07-17 (M4): wind-down distress policy (strategy:).
            winddown_alert_minutes=float(stra.get("winddown_alert_minutes", 30)),
            winddown_alert_move=_dec(stra, "winddown_alert_move", Decimal("0.05")),
            winddown_escalation=str(stra.get("winddown_escalation", "none")),
            fees=fee_schedules,
            # 2026-07-17 (DEPLOY): HEALTH_PORT (fly sets it) force-enables the
            # endpoint and overrides the configured port.
            health_enabled=bool(health_cfg.get("enabled", False))
            or "HEALTH_PORT" in os.environ,
            health_port=int(os.environ.get("HEALTH_PORT", health_cfg.get("port", 8080))),
            raw=data,
        )

    def credentials(self) -> Credentials:
        return Credentials(
            key_id=os.environ.get("KALSHI_API_KEY_ID"),
            private_key_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH"),
            private_key_inline=os.environ.get("KALSHI_PRIVATE_KEY"),
        )
