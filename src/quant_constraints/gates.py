"""Composable admission gates. Each check is read-only."""

import math
from datetime import date, datetime, time, timedelta

from .calendar import NY, SessionCalendar
from .config import ConstraintConfig
from .events import EarningsEvent, EarningsProvider
from .models import ALLOW, Action, Decision, Intent, Kind, MarketContext, State, aware


def reject(code: str, reason: str = "", **data: object) -> Decision:
    return Decision(Action.REJECT, code, reason, data=data)


class SessionGate:
    def __init__(self, config: ConstraintConfig, calendar: SessionCalendar):
        self.config, self.calendar = config, calendar

    def check(self, intent: Intent, state: State, context: MarketContext) -> Decision:
        aware(context.asof)
        if context.data_frequency not in {"1m", "5m", "15m", "30m", "irregular"}:
            raise ValueError("Precise session constraints require intraday data (30m or finer)")
        bounds = self.calendar.bounds(context.asof)
        if bounds is None or not bounds[0] <= context.asof < bounds[1]:
            action = (
                Action.DEFER
                if intent.kind is Kind.RISK or context.phase == "fill"
                else Action.REJECT
            )
            return Decision(
                action, "outside_rth", "Wait for an observed price in the next legal session"
            )
        if intent.quantity > 0 and not (
            bounds[0] + timedelta(minutes=self.config.entry_delay_minutes)
            <= context.asof
            < bounds[1] - timedelta(minutes=self.config.entry_close_buffer_minutes)
        ):
            return Decision(
                Action.DEFER if context.phase == "fill" else Action.REJECT,
                "entry_window",
                "Buy window excludes opening and closing buffers",
            )
        return ALLOW


class AccountConstraint:
    def __init__(self, config: ConstraintConfig, calendar: SessionCalendar):
        self.config, self.calendar = config, calendar

    def check(self, intent: Intent, state: State, context: MarketContext) -> Decision:
        values = (
            intent.quantity,
            context.price,
            context.commission,
            state.settled_cash,
            state.equity,
            state.reserved_cash,
            state.drawdown,
        )
        if (
            not all(math.isfinite(v) for v in values)
            or intent.quantity == 0
            or context.price <= 0
            or context.commission < 0
            or state.settled_cash < 0
            or state.equity <= 0
            or state.reserved_cash < 0
            or not 0 <= state.drawdown <= 1
        ):
            return reject("invalid_account_input")
        if intent.kind in {Kind.RISK, Kind.REDUCE} and intent.quantity > 0:
            return reject(
                "risk_buy_not_allowed" if intent.kind is Kind.RISK else "intent_direction"
            )
        if intent.quantity > 0 and state.missing_marks:
            return Decision(
                Action.DEFER,
                "portfolio_marks_missing",
                data={"assets": sorted(state.missing_marks)},
            )
        holding = state.holdings.get(intent.asset)
        quantity = holding.quantity if holding else 0.0
        if intent.quantity < 0:
            if -intent.quantity + state.pending_sells.get(intent.asset, 0) > quantity + 1e-10:
                return reject("short_sale", "Sell exceeds uncommitted long shares")
            if intent.kind is not Kind.RISK and holding and holding.opened_at:
                days = self.calendar.distance(
                    holding.opened_at.astimezone(NY).date(), context.asof.astimezone(NY).date()
                )
                if days < self.config.minimum_hold_sessions:
                    return reject("minimum_hold", "Optional strategy holding period")
        required = intent.quantity * context.price + context.commission
        reserve = self.config.cash_reserve * state.equity if intent.quantity > 0 else 0
        available = state.settled_cash - state.reserved_cash
        if required > available + 1e-10:
            return reject("settled_cash", available=available, required=required)
        if required + reserve > available + 1e-10:
            return reject("cash_reserve", available=available, required=required, reserve=reserve)
        return ALLOW


class RebalanceGate:
    def __init__(self, config: ConstraintConfig, calendar: SessionCalendar):
        self.config, self.calendar = config, calendar

    def scheduled(self, state: State, asof: datetime) -> bool:
        day = asof.astimezone(NY).date()
        if self.calendar.session(day) is None:
            return False
        if self.config.rebalance_mode == "monthly":
            previous, following = self.calendar.shift(day, -1), self.calendar.shift(day, 1)
            return (
                (previous.month != day.month)
                if self.config.monthly_session == "first"
                else (following.month != day.month)
            )
        if self.config.rebalance_mode == "semi_monthly":
            first = self.calendar.on_or_after(day.replace(day=1))
            second = self.calendar.on_or_after(day.replace(day=16))
            return day in {first, second}
        if self.config.rebalance_anchor:
            anchor = self.calendar.on_or_after(date.fromisoformat(self.config.rebalance_anchor))
        elif state.anchor:
            anchor = self.calendar.on_or_after(state.anchor.astimezone(NY).date())
        else:
            raise ValueError("every_n_trading_days requires an explicit or observed anchor")
        distance = self.calendar.distance(anchor, day)
        return distance >= 0 and distance % self.config.rebalance_n == 0

    def check(self, intent: Intent, state: State, context: MarketContext) -> Decision:
        if intent.kind is Kind.REBALANCE:
            if (
                intent.rebalance_id is not None
                and intent.rebalance_id not in state.authorized_plans
            ):
                return reject("rebalance_plan_not_active")
            if intent.rebalance_id is None and not self.scheduled(state, context.asof):
                return reject("rebalance_schedule")
            current = state.holdings.get(intent.asset)
            current_weight = current.quantity * current.price / state.equity if current else 0
            target = intent.target_weight
            if target is None:
                target = current_weight + intent.quantity * context.price / state.equity
            if (
                intent.rebalance_id is None
                and abs(target - current_weight) < self.config.weight_change_threshold - 1e-12
            ):
                return reject(
                    "weight_deadband", "Adjustment below configured percentage-point threshold"
                )
        if (
            intent.quantity > 0
            and intent.asset not in state.holdings
            and context.phase == "submission"
        ):
            year, week, _ = context.asof.astimezone(NY).isocalendar()
            count = sum(
                t.astimezone(NY).isocalendar()[:2] == (year, week) for t in state.entry_times
            )
            if count >= self.config.weekly_entries:
                return reject("weekly_entries", accepted_entries=count)
        return ALLOW


class EarningsGate:
    def __init__(
        self, config: ConstraintConfig, calendar: SessionCalendar, provider: EarningsProvider
    ):
        self.config, self.calendar, self.provider = config, calendar, provider

    def sessions(self, event: EarningsEvent):
        raw = event.announcement_at.astimezone(NY).date()
        day = self.calendar.on_or_after(raw)
        trading_day = self.calendar.session(raw) is not None
        affected = self.calendar.shift(day, 1) if event.timing == "AMC" and trading_day else day
        exit_day = raw if event.timing == "AMC" and trading_day else self.calendar.shift(day, -1)
        session = self.calendar.session(exit_day)
        assert session is not None
        deadline = session.market_close - timedelta(
            minutes=self.config.liquidation_close_buffer_minutes
        )
        return day, affected, deadline

    def known(self, asset: str, asof: datetime):
        # Filter again at the boundary, including for external provider implementations.
        coverage, events = self.provider.snapshot(asset, asof)
        if coverage and coverage.available_at > asof:
            coverage = None
        return coverage, tuple(
            event for event in events if event.available_at <= asof and not event.cancelled
        )

    def active_event(self, asset: str, context: MarketContext) -> EarningsEvent | None:
        _, events = self.known(asset, context.asof)
        day = context.asof.astimezone(NY).date()
        for event in events:
            _, affected, _ = self.sessions(event)
            if (
                affected
                <= day
                <= self.calendar.shift(affected, self.config.post_earnings_sessions - 1)
                and context.asof >= event.announcement_at
            ):
                return event
        return None

    def check(self, intent: Intent, state: State, context: MarketContext) -> Decision:
        if intent.quantity < 0:
            return ALLOW
        if context.instrument == "plain_sector_etf":
            return ALLOW  # explicit PIT classification; no single-company earnings event
        coverage, events = self.known(intent.asset, context.asof)
        day = context.asof.astimezone(NY).date()
        lookahead = self.calendar.shift(day, self.config.earnings_blackout_sessions)
        horizon = datetime.combine(lookahead, time.max, NY)
        warning = ALLOW
        if coverage is None or coverage.missing or coverage.covered_until < horizon:
            if self.config.missing_earnings == "reject":
                return reject(
                    "earnings_coverage_missing", "Historical schedule coverage is required"
                )
            warning = Decision(
                Action.ALLOW,
                "earnings_coverage_unverified",
                "Explicit opt-out: earnings schedule was not verified",
            )
        for event in events:
            event_day, affected, _ = self.sessions(event)
            start = self.calendar.shift(event_day, -self.config.earnings_blackout_sessions)
            if day >= start and context.asof < event.announcement_at:
                return reject("earnings_blackout", event_id=event.event_id, source=event.source)
            end = self.calendar.shift(affected, self.config.post_earnings_sessions - 1)
            if start <= day <= end and (event.missing or event.timing == "UNKNOWN"):
                return reject("earnings_timing_unknown", event_id=event.event_id)
        event = self.active_event(intent.asset, context)
        if event:
            _, affected, _ = self.sessions(event)
            first = self.calendar.session(affected)
            assert first is not None
            if context.asof < first.market_open + timedelta(
                minutes=self.config.post_earnings_wait_minutes
            ):
                return reject("post_earnings_wait", event_id=event.event_id)
            fields = (context.spread, context.rvol, context.volume)
            if (
                any(v is None or not math.isfinite(v) for v in fields)
                or context.liquidity_available_at is None
                or context.liquidity_available_at > context.asof
            ):
                return Decision(
                    Action.DEFER if self.config.missing_liquidity == "defer" else Action.REJECT,
                    "earnings_liquidity_missing",
                    data={"event_id": event.event_id},
                )
            assert (
                context.spread is not None
                and context.rvol is not None
                and context.volume is not None
            )
            if (
                not 0 <= context.spread <= self.config.max_spread
                or context.rvol < self.config.minimum_rvol
                or context.volume <= 0
            ):
                return reject("earnings_liquidity", event_id=event.event_id)
        return warning


class PortfolioGate:
    def __init__(self, config: ConstraintConfig, earnings: EarningsGate):
        self.config, self.earnings = config, earnings

    def cap(self, asset: str, state: State, context: MarketContext) -> tuple[float, Decision]:
        cap = self.config.max_weight
        if context.instrument == "equity" and self.earnings.active_event(asset, context):
            cap *= self.config.event_position_multiplier
        if self.config.market_gates:
            if (
                context.vix is None
                or not math.isfinite(context.vix)
                or context.vix < 0
                or context.vix_available_at is None
                or context.vix_available_at > context.asof
            ):
                return cap, Decision(
                    Action.DEFER if self.config.missing_market == "defer" else Action.REJECT,
                    "market_data_missing",
                )
            if (
                context.vix >= self.config.vix_block_at
                or state.drawdown >= self.config.drawdown_block
            ):
                return cap, reject("market_additions_blocked")
            if context.vix >= self.config.vix_reduce_at:
                cap *= self.config.vix_position_multiplier
        return cap, ALLOW

    def check(self, intent: Intent, state: State, context: MarketContext) -> Decision:
        if intent.quantity < 0:
            return ALLOW
        if context.instrument not in {"equity", "plain_sector_etf"}:
            return reject("instrument_not_allowed", "Only ordinary stocks and plain sector ETFs")
        if context.instrument == "plain_sector_etf":
            if (
                context.spread is None
                or not math.isfinite(context.spread)
                or context.volume is None
                or not math.isfinite(context.volume)
                or context.liquidity_available_at is None
                or context.liquidity_available_at > context.asof
            ):
                return Decision(
                    Action.DEFER if self.config.missing_liquidity == "defer" else Action.REJECT,
                    "liquidity_missing",
                )
            if not 0 <= context.spread <= self.config.max_spread or context.volume <= 0:
                return reject("liquidity")
        cap, market = self.cap(intent.asset, state, context)
        if market.action is not Action.ALLOW:
            return market
        current = state.holdings.get(intent.asset)
        current_value = current.quantity * context.price if current else 0
        committed = state.pending_buys.get(intent.asset, 0)
        if current_value / state.equity > self.config.max_weight + 1e-12:
            return reject("overweight_drift", weight=current_value / state.equity)
        target = (current_value + committed + intent.quantity * context.price) / state.equity
        if (
            intent.target_weight is not None
            and context.phase in {"submission", "amendment"}
            and (
                not math.isfinite(intent.target_weight)
                or intent.target_weight < 0
                or abs(intent.target_weight - target) > 1e-8
            )
        ):
            return reject("target_quantity_mismatch", projected_weight=target)
        floor = intent.target_weight if intent.target_weight is not None else target
        # A partial fill is not a new target; ordinary price drift never creates orders.
        if (
            context.phase in {"submission", "amendment"}
            and (not current or intent.target_weight is not None)
            and 0 < floor < self.config.min_target_weight - 1e-12
        ):
            return reject("target_below_minimum")
        names = set(state.holdings) | set(state.pending_buys) | {intent.asset}
        if len(names) > self.config.max_names:
            return reject("max_names")
        sector = context.sector or (current.sector if current else None)
        unknown = (
            sector is None
            or any(h.sector is None for h in state.holdings.values())
            or any(
                asset != intent.asset and not state.pending_sectors.get(asset)
                for asset in state.pending_buys
            )
        )
        if unknown and self.config.unknown_industry == "reject":
            return reject("industry_missing")
        same_industry = sum(
            h.quantity * (context.price if asset == intent.asset else h.price)
            for asset, h in state.holdings.items()
            if h.sector == sector
        )
        same_industry += sum(
            value
            for asset, value in state.pending_buys.items()
            if asset == intent.asset or state.pending_sectors.get(asset) == sector
        )
        # Unknown pending industries are conservatively included in warn mode.
        same_industry += sum(
            value
            for asset, value in state.pending_buys.items()
            if asset != intent.asset and not state.pending_sectors.get(asset)
        )
        remaining = min(
            cap * state.equity - current_value - committed,
            self.config.industry_cap * state.equity - same_industry,
        )
        if remaining <= 1e-10:
            return reject("position_or_industry_cap")
        if intent.quantity * context.price > remaining + 1e-10:
            resized = remaining / context.price
            if (
                context.phase == "submission"
                and not current
                and (current_value + committed + remaining) / state.equity
                < self.config.min_target_weight - 1e-12
            ):
                return reject("cap_below_target_minimum")
            event = (
                self.earnings.active_event(intent.asset, context)
                if context.instrument == "equity"
                else None
            )
            code = (
                "earnings_position_cap"
                if event and remaining == cap * state.equity - current_value - committed
                else "position_or_industry_cap"
            )
            return Decision(
                Action.RESIZE,
                code,
                quantity=resized,
                data={"event_id": event.event_id} if event else {},
            )
        if unknown:
            return Decision(
                Action.ALLOW, "industry_unknown_warning", "Industry exposure is not verified"
            )
        return ALLOW
