"""Validated, opt-in strategic preferences separate from legal account rules."""

import math
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path
from typing import Literal

import yaml


@dataclass(frozen=True)
class ConstraintConfig:
    cash_reserve: float = 0.10
    settlement_cycle: Literal["historical", "T+1", "T+2"] = "historical"
    minimum_hold_sessions: int = 0
    entry_delay_minutes: int = 30
    entry_close_buffer_minutes: int = 30
    earnings_blackout_sessions: int = 2
    hold_through_earnings: bool = False
    missing_earnings: Literal["reject", "allow"] = "reject"
    missing_earnings_position: Literal["liquidate", "hold"] = "liquidate"
    missing_liquidity: Literal["reject", "defer"] = "reject"
    missing_market: Literal["reject", "defer"] = "reject"
    missing_price: Literal["error", "defer"] = "error"
    liquidation_close_buffer_minutes: int = 30
    post_earnings_wait_minutes: int = 60
    post_earnings_sessions: int = 1
    max_spread: float = 0.0015
    minimum_rvol: float = 1.8
    event_position_multiplier: float = 0.5
    min_target_weight: float = 0.10
    max_weight: float = 0.25
    preferred_min_names: int = 4
    max_names: int = 6
    equity_target: float = 0.90
    industry_cap: float = 0.40
    unknown_industry: Literal["reject", "warn"] = "reject"
    drift_reduction: Literal["next_session", "rebalance", "disabled"] = "next_session"
    rebalance_mode: Literal["every_n_trading_days", "monthly", "semi_monthly"] = (
        "every_n_trading_days"
    )
    rebalance_anchor: str | None = None
    rebalance_n: int = 10
    monthly_session: Literal["first", "last"] = "first"
    weight_change_threshold: float = 0.03
    weekly_entries: int = 4
    market_gates: bool = False
    vix_reduce_at: float = 20.0
    vix_block_at: float = 30.0
    vix_position_multiplier: float = 0.5
    drawdown_block: float = 0.10
    drawdown_reduce: float = 0.15
    drawdown_reduction_fraction: float | None = None

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(item.default, float) and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"Invalid {item.name}")
        if self.drawdown_reduction_fraction is not None and (
            isinstance(self.drawdown_reduction_fraction, bool)
            or not isinstance(self.drawdown_reduction_fraction, (float, int))
            or not math.isfinite(self.drawdown_reduction_fraction)
        ):
            raise ValueError("Invalid drawdown reduction fraction")
        choices = {
            "settlement_cycle": {"historical", "T+1", "T+2"},
            "unknown_industry": {"reject", "warn"},
            "drift_reduction": {"next_session", "rebalance", "disabled"},
            "rebalance_mode": {"every_n_trading_days", "monthly", "semi_monthly"},
            "monthly_session": {"first", "last"},
            "missing_earnings": {"reject", "allow"},
            "missing_earnings_position": {"liquidate", "hold"},
            "missing_liquidity": {"reject", "defer"},
            "missing_market": {"reject", "defer"},
            "missing_price": {"error", "defer"},
        }
        for name, allowed in choices.items():
            if not isinstance(getattr(self, name), str) or getattr(self, name) not in allowed:
                raise ValueError(f"Invalid {name}")
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, float) and (not math.isfinite(value) or value < 0):
                raise ValueError(f"Invalid {item.name}")
        if self.rebalance_anchor is not None:
            if not isinstance(self.rebalance_anchor, str):
                raise ValueError("rebalance_anchor must be an ISO date")
            date.fromisoformat(self.rebalance_anchor)
        for name in ("hold_through_earnings", "market_gates"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        for name in (
            "minimum_hold_sessions",
            "entry_delay_minutes",
            "entry_close_buffer_minutes",
            "earnings_blackout_sessions",
            "liquidation_close_buffer_minutes",
            "post_earnings_wait_minutes",
            "post_earnings_sessions",
            "preferred_min_names",
            "max_names",
            "rebalance_n",
            "weekly_entries",
        ):
            value = getattr(self, name)
            minimum = (
                1
                if name
                in {
                    "post_earnings_sessions",
                    "preferred_min_names",
                    "max_names",
                    "rebalance_n",
                    "weekly_entries",
                }
                else 0
            )
            if type(value) is not int or value < minimum:
                raise ValueError(f"Invalid {name}")
        fractions = (
            "cash_reserve",
            "max_spread",
            "event_position_multiplier",
            "min_target_weight",
            "max_weight",
            "equity_target",
            "industry_cap",
            "weight_change_threshold",
            "vix_position_multiplier",
            "drawdown_block",
            "drawdown_reduce",
        )
        for name in fractions:
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (float, int))
                or not 0 <= value <= 1
            ):
                raise ValueError(f"{name} must be a fraction in [0, 1]")
        if not 0 < self.min_target_weight <= self.max_weight <= self.industry_cap:
            raise ValueError("Require 0 < min_target_weight <= max_weight <= industry_cap")
        if (
            self.preferred_min_names > self.max_names
            or self.equity_target > 1 - self.cash_reserve + 1e-12
        ):
            raise ValueError("Inconsistent name count or equity/cash targets")
        if (
            not 0 <= self.vix_reduce_at < self.vix_block_at
            or not 0 <= self.drawdown_block < self.drawdown_reduce <= 1
        ):
            raise ValueError("Invalid market gate thresholds")
        if (
            self.drawdown_reduction_fraction is not None
            and not 0 < self.drawdown_reduction_fraction <= 1
        ):
            raise ValueError("Invalid drawdown reduction fraction")

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ConstraintConfig":
        raw = yaml.safe_load(Path(path).read_text())
        if not isinstance(raw, dict):
            raise ValueError("Configuration must be a mapping")
        unknown = raw.keys() - {item.name for item in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown constraint settings: {sorted(unknown)}")
        return cls(**raw)
