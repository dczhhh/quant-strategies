"""Priority, audit and predefined risk reductions. Never creates a buy intent."""

from dataclasses import asdict
from datetime import timedelta

from .calendar import NY, SessionCalendar
from .config import ConstraintConfig
from .events import EarningsProvider
from .gates import (
    AccountConstraint,
    EarningsGate,
    PortfolioGate,
    RebalanceGate,
    SessionGate,
    reject,
)
from .models import ALLOW, Action, Audit, Decision, Intent, Kind, MarketContext, RiskRequest, State


class ConstraintController:
    def __init__(self, config: ConstraintConfig, earnings: EarningsProvider):
        self.config = config
        self.calendar = SessionCalendar()
        self.session = SessionGate(config, self.calendar)
        self.account = AccountConstraint(config, self.calendar)
        self.earnings = EarningsGate(config, self.calendar, earnings)
        self.portfolio = PortfolioGate(config, self.earnings)
        self.rebalance = RebalanceGate(config, self.calendar)
        self.audit: list[Audit] = []

    def check(self, intent: Intent, state: State, market_context: MarketContext) -> Decision:
        context = market_context
        # Even an outside-session risk request must not queue a short or invalid order.
        session = self.session.check(intent, state, context)
        account = self.account.check(intent, state, context)
        if account.action is not Action.ALLOW:
            decision = account
        elif session.action is not Action.ALLOW:
            decision = session
        elif intent.kind is Kind.RISK and intent.quantity > 0:
            decision = reject("risk_buy_not_allowed")
        elif intent.quantity < 0:
            decision = (
                self.rebalance.check(intent, state, context)
                if intent.kind is Kind.REBALANCE
                else ALLOW
            )
        else:
            decision = ALLOW
            warning = ALLOW
            for gate in (self.earnings, self.rebalance, self.portfolio):
                decision = gate.check(intent, state, context)
                if decision.action is not Action.ALLOW:
                    break
                if decision.code != "allowed":
                    warning = decision
            if decision.action is Action.ALLOW and warning.code != "allowed":
                decision = warning
        self.audit.append(
            Audit(
                context.asof,
                intent.asset,
                intent.order_id,
                intent.kind.value,
                intent.order_type,
                context.phase,
                state.settled_cash - state.reserved_cash,
                max(0, intent.quantity * context.price + context.commission),
                decision,
            )
        )
        return decision

    def risk_requests(self, state: State, context: MarketContext) -> tuple[RiskRequest, ...]:
        requests = []
        day = context.asof.astimezone(NY).date()
        for asset, holding in state.holdings.items():
            reason, quantity = "", 0.0
            coverage, events = self.earnings.known(asset, context.asof)
            if not self.config.hold_through_earnings:
                if (
                    coverage is None
                    or coverage.missing
                    or coverage.covered_until < context.asof + timedelta(days=1)
                ) and self.config.missing_earnings_position == "liquidate":
                    reason, quantity = "earnings_coverage_missing_exit", holding.quantity
                for event in events:
                    _, affected, deadline = self.earnings.sessions(event)
                    end = self.calendar.shift(affected, self.config.post_earnings_sessions - 1)
                    was_exposed = (
                        holding.opened_at is None or holding.opened_at < event.announcement_at
                    )
                    if was_exposed and context.asof >= deadline and day <= end:
                        reason, quantity = "earnings_predefined_exit", holding.quantity
                        bounds = self.calendar.bounds(deadline)
                        if bounds and context.asof >= bounds[1]:
                            self.audit.append(
                                Audit(
                                    context.asof,
                                    asset,
                                    event.event_id,
                                    Kind.RISK.value,
                                    "market",
                                    "monitor",
                                    state.settled_cash,
                                    0,
                                    Decision(
                                        Action.DEFER,
                                        "earnings_exit_window_missed",
                                        data={
                                            "deadline": deadline,
                                            "available_at": event.available_at,
                                            "source": event.source,
                                        },
                                    ),
                                )
                            )
            value = holding.quantity * holding.price
            if not state.missing_marks and value > self.config.max_weight * state.equity + 1e-10:
                self.audit.append(
                    Audit(
                        context.asof,
                        asset,
                        "drift",
                        Kind.RISK.value,
                        "market",
                        "monitor",
                        state.settled_cash,
                        0,
                        Decision(
                            Action.DEFER, "overweight_drift", data={"weight": value / state.equity}
                        ),
                    )
                )
                should_reduce = self.config.drift_reduction == "next_session" or (
                    self.config.drift_reduction == "rebalance"
                    and self.rebalance.scheduled(state, context.asof)
                )
                if should_reduce and not reason:
                    reason, quantity = (
                        "overweight_predefined_reduction",
                        (value - self.config.max_weight * state.equity) / holding.price,
                    )
            if (
                not state.missing_marks
                and self.config.market_gates
                and state.drawdown >= self.config.drawdown_reduce
            ):
                fraction = self.config.drawdown_reduction_fraction
                if fraction is None:
                    self.audit.append(
                        Audit(
                            context.asof,
                            asset,
                            "drawdown",
                            Kind.RISK.value,
                            "market",
                            "monitor",
                            state.settled_cash,
                            0,
                            reject("drawdown_reduction_plan_missing"),
                        )
                    )
                elif not reason:
                    reason, quantity = "drawdown_predefined_reduction", holding.quantity * fraction
            if reason:
                requests.append(RiskRequest(asset, quantity, reason))
        return tuple(requests)

    def check_targets(self, targets: dict[str, float], sectors: dict[str, str]) -> Decision:
        """Validate a complete external allocation without submitting any orders."""
        if not self.config.preferred_min_names <= len(targets) <= self.config.max_names:
            return reject("target_name_count")
        if any(
            not self.config.min_target_weight <= weight <= self.config.max_weight
            for weight in targets.values()
        ):
            return reject("target_weight_range")
        if sum(targets.values()) > self.config.equity_target + 1e-12:
            return reject("target_equity_budget")
        industry: dict[str, float] = {}
        for asset, weight in targets.items():
            sector = sectors.get(asset)
            if sector is None:
                if self.config.unknown_industry == "reject":
                    return reject("industry_missing")
                continue
            industry[sector] = industry.get(sector, 0) + weight
        if any(weight > self.config.industry_cap + 1e-12 for weight in industry.values()):
            return reject("industry_cap")
        if targets.keys() - sectors.keys():
            return Decision(Action.ALLOW, "industry_unknown_warning")
        return ALLOW

    def audit_records(self) -> list[dict]:
        return [asdict(record) for record in self.audit]

    def event_statistics(self) -> dict[str, float | int]:
        buys = [
            a
            for a in self.audit
            if a.order_kind in {Kind.ENTRY.value, Kind.ADD.value, Kind.REBALANCE.value}
            and a.phase == "submission"
        ]
        triggered = sum(a.decision.code.startswith(("earnings_", "post_earnings_")) for a in buys)
        return {
            "buy_checks": len(buys),
            "earnings_gate_triggers": triggered,
            "earnings_gate_trigger_rate": triggered / len(buys) if buys else 0.0,
        }
