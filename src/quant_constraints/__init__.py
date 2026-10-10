"""Opt-in US cash account constraints. This package contains no buy signals."""

from .adapter import ConstrainedBroker, broker_factory, cash_backtest_config, constrained_engine
from .config import ConstraintConfig
from .controller import ConstraintController
from .events import EarningsCoverage, EarningsEvent, EarningsProvider, InMemoryEarningsProvider
from .fees import FeeBreakdown, FeeExecutionContext, FeeRecord, IBKRProTieredUSStock, RegulatoryRate
from .gates import AccountConstraint, EarningsGate, PortfolioGate, RebalanceGate, SessionGate
from .models import (
    Action,
    Audit,
    Decision,
    Holding,
    Intent,
    Kind,
    MarketContext,
    RiskRequest,
    State,
)
from .plans import RebalancePlan

__all__ = [
    "AccountConstraint",
    "Action",
    "Audit",
    "ConstrainedBroker",
    "ConstraintConfig",
    "ConstraintController",
    "Decision",
    "EarningsCoverage",
    "EarningsEvent",
    "EarningsGate",
    "EarningsProvider",
    "FeeBreakdown",
    "FeeExecutionContext",
    "FeeRecord",
    "IBKRProTieredUSStock",
    "RegulatoryRate",
    "Holding",
    "InMemoryEarningsProvider",
    "Intent",
    "Kind",
    "MarketContext",
    "PortfolioGate",
    "RebalanceGate",
    "RebalancePlan",
    "RiskRequest",
    "SessionGate",
    "State",
    "broker_factory",
    "cash_backtest_config",
    "constrained_engine",
]
