"""Validated, opt-in strategic preferences separate from legal account rules."""

import math
from dataclasses import dataclass, fields
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import yaml

from .dividend_tax import DividendTaxQualification, DividendTaxRule
from .macro import MACRO_TYPES


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
    buy_time_in_force: Literal["DAY", "GTD"] = "DAY"
    max_defer_sessions: int = 2
    liquidation_close_buffer_minutes: int = 30
    post_earnings_wait_minutes: int = 60
    post_earnings_sessions: int = 1
    max_spread: float = 0.0015
    minimum_rvol: float = 1.8
    event_position_multiplier: float = 0.5
    min_target_weight: float = 0.10
    max_weight: float = 0.25
    preferred_min_names: int = 4
    allow_defensive_underinvested: bool = False
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
    rebalance_plan_enabled: bool = False
    rebalance_plan_sessions: int = 5
    rebalance_plan_max_price_change: float = 0.05
    weight_change_threshold: float = 0.03
    weekly_entries: int = 4
    market_gates: bool = False
    vix_reduce_at: float = 20.0
    vix_block_at: float = 30.0
    vix_position_multiplier: float = 0.5
    drawdown_block: float = 0.10
    drawdown_reduce: float = 0.15
    drawdown_reduction_fraction: float | None = None
    pricing_plan: Literal["ibkr_pro_tiered", "custom"] = "ibkr_pro_tiered"
    fee_history_mode: Literal["current_snapshot_backcast", "strict_historical"] = (
        "current_snapshot_backcast"
    )
    fee_snapshot_date: str = "2026-10-10"
    fee_initial_monthly_volume: float = 0.0
    fee_initial_month: str | None = None
    fee_unknown_venue_per_share: float = 0.0035
    fee_unknown_venue_rate: float = 0.0035
    slippage_mode: Literal["regime", "configured"] = "regime"
    slippage_regular_bps: float = 2.0
    slippage_early_close_bps: float = 3.0
    slippage_earnings_bps: float = 5.0
    slippage_missing_earnings: Literal["stress", "regular", "error"] = "stress"
    corporate_actions_enabled: bool = False
    split_order_policy: Literal["adjust", "cancel"] = "adjust"
    execution_data_mode: Literal["raw_execution"] = "raw_execution"
    signal_data_mode: Literal["raw_execution", "split_adjusted_signal", "total_return_signal"] = (
        "split_adjusted_signal"
    )
    dividend_withholding_rate: float = 0.0
    dividend_tax_scenario: str = "gross_no_withholding"
    dividend_tax_profile: Literal["legacy", "cn_mainland_individual_treaty", "explicit_rules"] = (
        "legacy"
    )
    dividend_tax_rules: tuple[DividendTaxRule, ...] = ()
    dividend_tax_qualification: DividendTaxQualification | None = None
    dividend_tax_conflict_policy: Literal["record_actual", "reject"] = "record_actual"
    unknown_dividend_tax_policy: Literal["reject"] = "reject"
    capital_gains_tax_mode: Literal["none"] = "none"
    macro_events_enabled: bool = False
    macro_event_types: tuple[str, ...] = (
        "CPI",
        "NFP",
        "PCE",
        "PPI",
        "FOMC_STATEMENT",
        "FOMC_PRESS",
        "ISM",
    )
    missing_macro_calendar: Literal["reject_new_entries", "explicit_opt_out"] = "reject_new_entries"
    macro_major_wait_minutes: int = 60
    macro_ppi_wait_minutes: int = 45
    macro_fomc_statement_lead_minutes: int = 30
    macro_fomc_press_lead_minutes: int = 60
    macro_fomc_next_session: bool = False
    macro_fomc_next_wait_minutes: int = 60
    macro_pre_release_minutes: int = 15
    macro_post_release_minutes: int = 30
    macro_slippage_bps: float = 10.0

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
            "dividend_tax_profile": {"legacy", "cn_mainland_individual_treaty", "explicit_rules"},
            "dividend_tax_conflict_policy": {"record_actual", "reject"},
            "unknown_dividend_tax_policy": {"reject"},
            "capital_gains_tax_mode": {"none"},
            "missing_macro_calendar": {"reject_new_entries", "explicit_opt_out"},
            "split_order_policy": {"adjust", "cancel"},
            "execution_data_mode": {"raw_execution"},
            "signal_data_mode": {"raw_execution", "split_adjusted_signal", "total_return_signal"},
            "pricing_plan": {"ibkr_pro_tiered", "custom"},
            "fee_history_mode": {"current_snapshot_backcast", "strict_historical"},
            "slippage_mode": {"regime", "configured"},
            "slippage_missing_earnings": {"stress", "regular", "error"},
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
            "buy_time_in_force": {"DAY", "GTD"},
        }
        for name, allowed in choices.items():
            if not isinstance(getattr(self, name), str) or getattr(self, name) not in allowed:
                raise ValueError(f"Invalid {name}")
        if self.fee_snapshot_date != "2026-10-10":
            raise ValueError("Only the documented 2026-10-10 fee snapshot is supported")
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, float) and (not math.isfinite(value) or value < 0):
                raise ValueError(f"Invalid {item.name}")
        if self.fee_initial_month is not None:
            if not isinstance(self.fee_initial_month, str):
                raise ValueError("fee_initial_month must be YYYY-MM")
            parsed = date.fromisoformat(self.fee_initial_month + "-01")
            if parsed.strftime("%Y-%m") != self.fee_initial_month:
                raise ValueError("fee_initial_month must be YYYY-MM")
        if self.fee_initial_monthly_volume and self.fee_initial_month is None:
            raise ValueError("Nonzero initial fee volume requires fee_initial_month")
        if self.rebalance_anchor is not None:
            if not isinstance(self.rebalance_anchor, str):
                raise ValueError("rebalance_anchor must be an ISO date")
            date.fromisoformat(self.rebalance_anchor)
        for name in (
            "macro_events_enabled",
            "macro_fomc_next_session",
            "corporate_actions_enabled",
            "hold_through_earnings",
            "market_gates",
            "allow_defensive_underinvested",
            "rebalance_plan_enabled",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        for name in (
            "macro_major_wait_minutes",
            "macro_ppi_wait_minutes",
            "macro_fomc_statement_lead_minutes",
            "macro_fomc_press_lead_minutes",
            "macro_fomc_next_wait_minutes",
            "macro_pre_release_minutes",
            "macro_post_release_minutes",
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
            "max_defer_sessions",
            "rebalance_plan_sessions",
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
                    "max_defer_sessions",
                    "rebalance_plan_sessions",
                }
                else 0
            )
            if type(value) is not int or value < minimum:
                raise ValueError(f"Invalid {name}")
        fractions = (
            "dividend_withholding_rate",
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
            "rebalance_plan_max_price_change",
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
        if (
            not isinstance(self.dividend_tax_scenario, str)
            or not self.dividend_tax_scenario.strip()
        ):
            raise ValueError("An explicit dividend tax scenario is required")
        if self.dividend_withholding_rate and self.dividend_tax_scenario == "gross_no_withholding":
            raise ValueError("Nonzero withholding requires a named investor tax scenario")
        if not isinstance(self.dividend_tax_rules, tuple) or any(
            not isinstance(rule, DividendTaxRule) for rule in self.dividend_tax_rules
        ):
            raise ValueError("dividend_tax_rules must be an immutable tuple of DividendTaxRule")
        if self.dividend_tax_qualification is not None and not isinstance(
            self.dividend_tax_qualification, DividendTaxQualification
        ):
            raise ValueError("Invalid dividend tax qualification")
        if self.dividend_tax_profile != "legacy" and (
            not self.dividend_tax_rules
            or self.dividend_withholding_rate
            or self.dividend_tax_scenario == "gross_no_withholding"
        ):
            raise ValueError("Country tax profiles require explicit versioned rules/scenario")
        if (
            not isinstance(self.macro_event_types, tuple)
            or not self.macro_event_types
            or len(set(self.macro_event_types)) != len(self.macro_event_types)
            or not set(self.macro_event_types) <= MACRO_TYPES
        ):
            raise ValueError("Invalid macro event types")

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ConstraintConfig":
        raw = yaml.safe_load(Path(path).read_text())
        if not isinstance(raw, dict):
            raise ValueError("Configuration must be a mapping")
        unknown = raw.keys() - {item.name for item in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown constraint settings: {sorted(unknown)}")

        def typed_terms(value):
            terms = dict(value)
            for name in ("effective_from", "effective_to"):
                if isinstance(terms[name], str):
                    terms[name] = date.fromisoformat(terms[name])
            for name in ("known_at", "verified_at"):
                if name in terms and isinstance(terms[name], str):
                    terms[name] = datetime.fromisoformat(terms[name])
            return terms

        if "dividend_tax_rules" in raw:
            raw["dividend_tax_rules"] = tuple(
                DividendTaxRule(**typed_terms(rule)) for rule in raw["dividend_tax_rules"]
            )
        if raw.get("dividend_tax_qualification") is not None:
            raw["dividend_tax_qualification"] = DividendTaxQualification(
                **typed_terms(raw["dividend_tax_qualification"])
            )
        if "macro_event_types" in raw:
            raw["macro_event_types"] = tuple(raw["macro_event_types"])
        return cls(**raw)
