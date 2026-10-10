"""Opt-in backtest adapter; core cash, position and order state remain canonical."""

import copy
import math
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import fields, replace
from datetime import datetime
from types import MappingProxyType

from ml4t.backtest import BacktestConfig, Broker, Engine
from ml4t.backtest.config import (
    CommissionType,
    DataFrequency,
    ExecutionPrice,
    FillOrdering,
    SlippageType,
)
from ml4t.backtest.core.shared import SubmitOrderOptions
from ml4t.backtest.execution.fill_executor import FillExecutor
from ml4t.backtest.models import calculate_commission, calculate_slippage
from ml4t.backtest.types import ExecutionMode, Order, OrderSide, OrderStatus, OrderType

from .calendar import NY, settlement_date
from .controller import ConstraintController
from .corporate_actions import CorporateActionProcessor, CorporateActionProvider
from .fees import IBKRProTieredUSStock
from .fees.bridge import FeeCommissionBridge
from .models import Action, Audit, Decision, Holding, Intent, Kind, MarketContext, State, aware
from .plans import RebalancePlanManager
from .slippage import RegimeSlippage, SlippageQuote, SlippageRecord

ContextProvider = Callable[[datetime, str, str], MarketContext]


def cash_backtest_config(**changes) -> BacktestConfig:
    config = BacktestConfig.from_preset("us_cash_equities")
    defaults = {
        "data_frequency": DataFrequency.MINUTE_1,
        "execution_mode": ExecutionMode.NEXT_BAR,
        "execution_price": ExecutionPrice.OPEN,
        "cash_buffer_pct": 0.0,
        "fill_ordering": FillOrdering.EXIT_FIRST,
        # Nonzero fallback for a caller using this BacktestConfig without the
        # constrained factory. The factory installs the full explicit plan.
        "commission_type": CommissionType.PER_SHARE,
        "commission_per_share": 0.0035,
        "commission_minimum": 0.35,
        "slippage_type": SlippageType.PERCENTAGE,
        "slippage_rate": 0.0002,
    }
    defaults.update(changes)
    return replace(config, **defaults)


class ConstraintFillExecutor(FillExecutor):
    def execute(self, order: Order, base_price: float) -> bool:
        broker = self.broker
        assert isinstance(broker, ConstrainedBroker)
        if order.status is not OrderStatus.PENDING:
            return True
        if broker.expire_order(order):
            return True
        intent = broker.intent_for(order)
        context = broker.context_for(order.asset, base_price, "fill")
        state = broker.constraint_state(order.order_id, phase="fill")
        if order.asset not in self.market.opens and order.asset not in self.market.prices:
            broker.audit_deferred(order, "price_missing")
            return False
        due = broker.deferred_until.get(order.order_id)
        if due and context.asof.astimezone(NY).date() < due:
            broker.audit_deferred(order, "risk_wait_session")
            return False
        session = broker.controller.session.check(intent, state, context)
        if session.action is not Action.ALLOW:
            broker.controller.check(intent, state, context)
            if session.action is Action.DEFER:
                broker.audit_deferred(order, session.code)
            if intent.kind is Kind.RISK:
                broker.deferred_risk.add(order.order_id)
            if session.action is Action.REJECT:
                order.reject(session.reason, session.code)
                order._reserved_cash = 0
                return True
            return False
        if order.order_id in broker.deferred_risk:
            # A deferred trigger is not a guaranteed execution price. Use observed open.
            price = self.market.opens.get(order.asset)
            if price is None:
                return False
            base_price = price
            order._risk_fill_price = None
        preliminary = broker.controller.check(intent, state, context)
        if preliminary.action is Action.DEFER:
            broker.audit_deferred(order, preliminary.code)
            return False
        broker.checking_actual_fill = True
        try:
            with (
                (
                    broker.fee_bridge.bind(order, actual=True)
                    if broker.fee_bridge is not None
                    else nullcontext()
                ),
                (
                    broker.regime_slippage.bind(
                        broker.context_for(order.asset, base_price, "slippage"), actual=True
                    )
                    if broker.regime_slippage is not None
                    else nullcontext()
                ),
            ):
                completed = super().execute(order, base_price)
        finally:
            broker.checking_actual_fill = False
        broker.refresh_brackets()
        if order.status is OrderStatus.REJECTED and order.order_id in broker.fill_rejections:
            order._rejection_code = broker.fill_rejections.pop(order.order_id)
        broker.audit_order_transition(order)
        return completed


class ConstrainedBroker(Broker):
    """Configure via broker_factory; no changes to unconfigured upstream Broker."""

    def configure_constraints(
        self,
        controller: ConstraintController,
        context_provider: ContextProvider,
        config: BacktestConfig,
        fee_model: IBKRProTieredUSStock | None = None,
        corporate_action_provider: CorporateActionProvider | None = None,
    ) -> None:
        if hasattr(self, "controller"):
            raise ValueError("Constraint broker is already configured")
        if not self.us_cash_account or self.cash_buffer_pct != 0:
            raise ValueError("Constraints require strict us_cash_account and cash_buffer_pct=0")
        if config.resolved_data_frequency not in {
            DataFrequency.MINUTE_1,
            DataFrequency.MINUTE_5,
            DataFrequency.MINUTE_15,
            DataFrequency.MINUTE_30,
            DataFrequency.IRREGULAR,
        }:
            raise ValueError(
                "Constraints require intraday data, 30m or finer; daily OHLC is unsupported"
            )
        if config.enforce_sessions:
            raise ValueError(
                "Use constraint session checks; retain outside-session observations for deferred risk"
            )
        if self._contract_specs:
            raise ValueError(
                "Contract multipliers/derivatives are unsupported by the equity constraint profile"
            )
        if self.fill_ordering is not FillOrdering.EXIT_FIRST:
            raise ValueError("Constraint profile requires exit_first fill ordering")
        if controller.config.corporate_actions_enabled != (corporate_action_provider is not None):
            raise ValueError(
                "Corporate actions require both opt-in config and an explicit provider"
            )
        self.controller = controller
        self.context_provider = context_provider
        if controller.config.pricing_plan == "ibkr_pro_tiered":
            settings = controller.config
            self.fee_model = fee_model or IBKRProTieredUSStock(
                initial_monthly_volume=settings.fee_initial_monthly_volume,
                initial_month=settings.fee_initial_month,
                unknown_venue_per_share=settings.fee_unknown_venue_per_share,
                unknown_venue_rate=settings.fee_unknown_venue_rate,
                history_mode=settings.fee_history_mode,
                snapshot_date=settings.fee_snapshot_date,
            )
            self.fee_bridge = FeeCommissionBridge(self, self.fee_model)
            self.commission_model = self.fee_bridge
            self.gatekeeper.commission_model = self.fee_bridge
        else:
            if fee_model is not None:
                raise ValueError("fee_model requires pricing_plan=ibkr_pro_tiered")
            self.fee_model = None
            self.fee_bridge = None
        self.regime_slippage = None
        if controller.config.slippage_mode == "regime":
            if (
                config.slippage_type not in {SlippageType.NONE, SlippageType.PERCENTAGE}
                or config.slippage_spread
                or config.slippage_spread_by_asset
                or config.slippage_fixed
                or config.slippage_rate not in {0, 0.0002}
                or self.market_impact_model is not None
                or self.stop_slippage_rate
                or config.execution_price in {ExecutionPrice.BID, ExecutionPrice.ASK}
            ):
                raise ValueError(
                    "Regime slippage replaces spread/impact costs; use slippage_mode=configured "
                    "for a separate explicit execution-cost scenario"
                )
            self.regime_slippage = RegimeSlippage(
                controller.config,
                controller.calendar,
                controller.earnings.provider,
                lambda asset: self.context_for(asset, 0, "slippage"),
                macro=controller.macro,
            )
            self.slippage_model = self.regime_slippage
        self.constraint_frequency = config.resolved_data_frequency.value
        self.constraint_kinds: dict[str, Kind] = {}
        self.constraint_targets: dict[str, float] = {}
        self.entry_times: list[datetime] = []
        self.constraint_anchor: datetime | None = None
        self.constraint_peak = self.initial_cash
        self.deferred_risk: set[str] = set()
        self.deferred_until: dict = {}
        self.fill_rejections: dict[str, str] = {}
        self.drawdown_plan_applied = False
        self.checking_actual_fill = False
        self.risk_monitor_submission = False
        self.deferred_rule_assets: set[str] = set()
        self.bracket_children: dict[str, tuple[str, str]] = {}
        self.order_validity: dict[str, datetime] = {}
        self.order_requested: dict[str, float] = {}
        self.order_permitted: dict[str, float] = {}
        self.order_audit_state: dict[str, tuple[float, OrderStatus]] = {}
        self.plan_manager = RebalancePlanManager(self)
        self._fill_executor = ConstraintFillExecutor(
            self,
            account=self.account,
            market=self._market_state,
            orders=self._order_state,
            risk=self._risk_state,
            journal=self._execution_journal,
            record_pnl=self._record_pnl_event,
        )
        self._fill_executor.fill_engine = self._fill_engine
        self._fill_engine.executor = self._fill_executor
        self.corporate_action_processor = (
            CorporateActionProcessor(self, corporate_action_provider)
            if corporate_action_provider is not None
            else None
        )

    def context_for(
        self,
        asset: str,
        price: float,
        phase: str,
        commission: float = 0,
        reference_price: float | None = None,
    ) -> MarketContext:
        timestamp = self._market_state.time
        assert timestamp is not None
        aware(timestamp)
        supplied = self.context_provider(timestamp, asset, phase)
        if supplied.asof != timestamp:
            raise ValueError("Context provider must return the current point-in-time snapshot")
        return replace(
            supplied,
            price=price,
            commission=commission,
            phase=phase,
            data_frequency=self.constraint_frequency,
            reference_price=reference_price,
        )

    def constraint_state(self, exclude: str = "", phase: str = "submission") -> State:
        market, orders = self._market_state, self._order_state
        assert market.time is not None
        holdings = {}
        missing_marks = set()
        for asset, position in self.account.positions.items():
            price = (market.opens if phase == "fill" else market.prices).get(asset)
            if price is None:
                if self.controller.config.missing_price == "error":
                    raise ValueError(f"Observed {phase} mark missing for held asset {asset}")
                price = market.last_prices.get(asset, position.current_price)
                if price is None or not math.isfinite(price) or price <= 0:
                    raise ValueError(f"No valid historical valuation for {asset}")
                missing_marks.add(asset)
            supplied = self.context_provider(market.time, asset, phase)
            holdings[asset] = Holding(
                position.quantity, price, supplied.sector, position.entry_time, supplied.instrument
            )
        equity = (
            self.cash
            + self.account._receivable_value
            + sum(h.quantity * h.price for h in holdings.values())
        )
        if not missing_marks:
            self.constraint_peak = max(self.constraint_peak, equity)
        buys: dict[str, float] = {}
        sells: dict[str, float] = {}
        protective: dict[str, tuple[str, float]] = {}
        excluded_order = self.get_order(exclude) if exclude else None
        excluded_group = excluded_order.parent_id if excluded_order else None
        for order in orders.pending:
            if order.status is not OrderStatus.PENDING or order.order_id == exclude:
                continue
            quantity = orders.partial_quantities.get(order.order_id, order.quantity)
            if order.side is OrderSide.BUY:
                price = order._reservation_price or market.prices.get(order.asset, 0)
                buys[order.asset] = buys.get(order.asset, 0) + quantity * price
            else:
                if order.parent_id in self.bracket_children:
                    if order.parent_id != excluded_group:
                        previous = protective.get(order.parent_id, (order.asset, 0))[1]
                        protective[order.parent_id] = (order.asset, max(previous, quantity))
                else:
                    sells[order.asset] = sells.get(order.asset, 0) + quantity
        for asset, quantity in protective.values():
            sells[asset] = sells.get(asset, 0) + quantity
        assert self._cash_account_rules is not None
        return State(
            self.settled_cash,
            equity,
            holdings,
            self._cash_account_rules.reserved_cash(exclude),
            buys,
            sells,
            tuple(self.entry_times),
            self.constraint_anchor,
            1 - equity / self.constraint_peak if self.constraint_peak > 0 else 0.0,
            {asset: self.context_provider(market.time, asset, phase).sector for asset in buys},
            frozenset(missing_marks),
            self.plan_manager.authorized(market.time),
        )

    @property
    def rebalance_plans(self):
        return MappingProxyType(self.plan_manager.records)

    @property
    def fee_records(self):
        return self.fee_model.records if self.fee_model is not None else ()

    def fee_statistics(self):
        fields = (
            "broker_commission",
            "exchange_ecn_fees_or_rebates",
            "clearing_fees",
            "regulatory_fees",
            "pass_through_fees",
            "total_fees",
        )
        return {
            name: sum(getattr(record.fees, name) for record in self.fee_records) for name in fields
        }

    @property
    def slippage_records(self):
        if self.regime_slippage is not None:
            return self.regime_slippage.records
        return tuple(
            SlippageRecord(
                f"configured/{index}",
                fill.order_id,
                fill.side.value,
                SlippageQuote(
                    fill.asset,
                    fill.timestamp,
                    fill.quantity,
                    fill.price - fill.slippage
                    if fill.side is OrderSide.BUY
                    else fill.price + fill.slippage,
                    "configured",
                    fill.slippage
                    / (
                        fill.price - fill.slippage
                        if fill.side is OrderSide.BUY
                        else fill.price + fill.slippage
                    )
                    * 10_000,
                    ("explicit configured upstream slippage model",),
                ),
            )
            for index, fill in enumerate(self._execution_journal.fills)
        )

    def slippage_statistics(self):
        regimes = {}
        for record in self.slippage_records:
            quote = record.quote
            group = regimes.setdefault(
                quote.slippage_regime, {"fills": 0, "quantity": 0.0, "slippage_amount": 0.0}
            )
            group["fills"] += 1
            group["quantity"] += quote.quantity
            group["slippage_amount"] += quote.slippage_amount
        return {
            "macro_slippage_cost": sum(
                q.quote.quantity * q.quote.reference_price * q.quote.macro_incremental_bps / 10_000
                for q in self.slippage_records
            ),
            "event_exposure": sum(
                any("macro event=" in item for item in q.quote.basis) for q in self.slippage_records
            ),
            "total_slippage": sum(q.quote.slippage_amount for q in self.slippage_records),
            "by_regime": regimes,
        }

    def execution_cost_statistics(self):
        scenarios = {}
        for record in self.fee_records:
            fee = record.fees
            key = (fee.fee_history_mode, fee.fee_snapshot_date, fee.rate_version)
            scenario = scenarios.setdefault(
                key,
                {
                    "fee_history_mode": fee.fee_history_mode,
                    "fee_snapshot_date": fee.fee_snapshot_date,
                    "historical_fee_proxy": fee.historical_fee_proxy,
                    "rate_version": fee.rate_version,
                    "executions": 0,
                    "sources": set(),
                    "assumptions": set(),
                },
            )
            scenario["executions"] += 1
            scenario["sources"].update(fee.sources)
            scenario["assumptions"].update(fee.assumptions)
        fees = self.fee_statistics()
        if self.fee_model is None:
            total = sum(fill.commission for fill in self._execution_journal.fills)
            fees.update(total_fees=total, custom_unclassified_fees=total)
        return {
            "fees": fees,
            "slippage": self.slippage_statistics(),
            "fee_scenarios": tuple(
                {
                    **scenario,
                    "sources": tuple(sorted(scenario["sources"])),
                    "assumptions": tuple(sorted(scenario["assumptions"])),
                }
                for scenario in scenarios.values()
            ),
        }

    def estimate_execution_price(self, asset, signed_quantity, price):
        if price <= 0:
            return price  # price gate handles missing observations
        amount = calculate_slippage(
            self.slippage_model,
            asset,
            abs(signed_quantity),
            price,
            self._market_state.volumes.get(asset),
        )
        return price + amount if signed_quantity > 0 else price - amount

    def estimate_order_fees(
        self, asset, signed_quantity, price, order_id="estimate", generation=None
    ):
        if self.fee_bridge is not None:
            return self.fee_bridge.estimate(asset, signed_quantity, price, order_id, generation)
        return calculate_commission(self.commission_model, asset, abs(signed_quantity), price)

    def reserve_cash_order(self, order, quantity=None):
        bridge = getattr(self, "fee_bridge", None)
        with bridge.bind(order) if bridge is not None else nullcontext():
            return super().reserve_cash_order(order, quantity)

    def create_rebalance_plan(
        self, target_weights, *, rebalance_id=None, valid_until=None, defensive_allocation=False
    ):
        return self.plan_manager.create(
            dict(target_weights),
            plan_id=rebalance_id,
            valid_until=valid_until,
            defensive_allocation=defensive_allocation,
        )

    def cancel_rebalance_plan(self, rebalance_id):
        return self.plan_manager.cancel(rebalance_id)

    def audit_plan_rejection(self, code: str) -> None:
        timestamp = self._market_state.time
        assert timestamp is not None
        self.controller.audit.append(
            Audit(
                timestamp,
                "portfolio",
                "targets",
                Kind.REBALANCE.value,
                "market",
                "plan",
                self.settled_cash,
                0,
                Decision(Action.REJECT, code),
            )
        )

    def order_deadline(self, kind, time_in_force, valid_until, rebalance_id, asset):
        now = self._market_state.time
        assert now is not None
        if kind is Kind.RISK:
            if valid_until is not None or time_in_force not in {None, "GTC"}:
                raise ValueError("Risk protection uses GTC and cannot inherit a buy expiry")
            return None
        plan_deadline = None
        if rebalance_id is not None:
            plan = self.plan_manager.records.get(rebalance_id)
            if plan is not None and plan.active:
                plan_deadline = plan.valid_until
        tif = time_in_force or self.controller.config.buy_time_in_force
        if tif not in {"DAY", "GTD"}:
            raise ValueError("Ordinary orders require DAY or GTD validity")
        calendar = self.controller.calendar
        day = calendar.on_or_after(now.astimezone(NY).date())
        if tif == "GTD":
            day = calendar.shift(day, self.controller.config.max_defer_sessions - 1)
        session = calendar.session(day)
        assert session is not None
        deadline = session.market_close
        if plan_deadline is not None:
            deadline = plan_deadline
        if valid_until is not None:
            deadline = min(deadline, aware(valid_until))
        # A company's event admission never spills into an unrelated session.
        context = self.context_for(asset, self._market_state.prices.get(asset, 0), "submission")
        event = (
            self.controller.earnings.active_event(asset, context)
            if context.instrument == "equity"
            else None
        )
        if event is not None:
            _, affected, _ = self.controller.earnings.sessions(event)
            end = calendar.session(
                calendar.shift(affected, self.controller.config.post_earnings_sessions - 1)
            )
            assert end is not None
            deadline = min(deadline, end.market_close)
        if deadline <= now:
            raise ValueError("Order valid_until must be in the future")
        return deadline

    def audit_order_event(
        self, order: Order, phase: str, decision: Decision, *, filled=0.0, status=None
    ):
        now = self._market_state.time
        assert now is not None
        execution = next(
            (
                record.quote
                for record in reversed(self.slippage_records)
                if filled > 0
                and record.order_id == order.order_id
                and record.quote.timestamp == now
            ),
            None,
        )
        self.controller.audit.append(
            Audit(
                now,
                order.asset,
                order.order_id,
                self.intent_for(order).kind.value,
                order.order_type.value,
                phase,
                self.settled_cash,
                0,
                decision,
                side=order.side.value,
                requested_quantity=self.order_requested.get(
                    order.order_id, order.requested_quantity or order.quantity
                ),
                permitted_quantity=self.order_permitted.get(order.order_id, 0),
                filled_quantity=filled,
                final_status=status or order.status.value,
                slippage_regime=execution.slippage_regime if execution else None,
                slippage_bps=execution.slippage_bps if execution else None,
                slippage_amount=execution.slippage_amount if execution else 0.0,
                slippage_basis=execution.basis if execution else (),
            )
        )

    def audit_order_transition(self, order: Order):
        previous = self.order_audit_state.get(order.order_id, (0.0, OrderStatus.PENDING))
        increment = order.filled_quantity - previous[0]
        if increment > 1e-10:
            self.audit_order_event(
                order, "execution", Decision(Action.ALLOW, "executed"), filled=increment
            )
        if order.status is not OrderStatus.PENDING and (
            order.status is not previous[1] or order.order_id not in self.order_audit_state
        ):
            self.audit_order_event(
                order,
                "terminal",
                Decision(
                    Action.REJECT if order.status is OrderStatus.REJECTED else Action.ALLOW,
                    order.rejection_code or order.status.value,
                ),
            )
        self.order_audit_state[order.order_id] = (order.filled_quantity, order.status)

    def audit_deferred(self, order: Order, code: str):
        self.audit_order_event(order, "deferred", Decision(Action.DEFER, code))

    def cancel_constraint_order(self, order_id: str, reason: str, *, expired: bool = False):
        order = self.get_order(order_id)
        result = super().cancel_order(order_id)
        if result and order is not None:
            order._reserved_cash = 0.0
            self.audit_order_event(
                order,
                "terminal",
                Decision(Action.REJECT, reason),
                status="expired" if expired else "canceled",
            )
            self.order_audit_state[order_id] = (order.filled_quantity, order.status)
        return result

    def expire_order(self, order: Order) -> bool:
        deadline = self.order_validity.get(order.order_id)
        now = self._market_state.time
        assert now is not None
        if deadline is not None and now >= deadline and order.status is OrderStatus.PENDING:
            if order.rebalance_id in self.plan_manager.records:
                self.plan_manager.cancel(order.rebalance_id, "rebalance_plan_expired", expired=True)
            else:
                self.cancel_constraint_order(order.order_id, "order_expired", expired=True)
                self.refresh_brackets()
            return True
        return False

    def expire_orders(self):
        now = self._market_state.time
        assert now is not None
        for identifier, plan in tuple(self.plan_manager.records.items()):
            if plan.active and now >= plan.valid_until:
                self.plan_manager.cancel(identifier, "rebalance_plan_expired", expired=True)
        for order in tuple(self._order_state.pending):
            self.expire_order(order)

    def submit_bracket(
        self,
        asset,
        quantity,
        take_profit,
        stop_loss,
        entry_type=OrderType.MARKET,
        entry_limit=None,
        validate_prices=True,
    ):
        """Register dormant protective orders before a parent can fill; OCO shares are shared."""
        reference = entry_limit if entry_limit is not None else self._market_state.prices.get(asset)
        if (
            quantity <= 0
            or reference is None
            or not all(
                math.isfinite(value) and value > 0 for value in (reference, take_profit, stop_loss)
            )
        ):
            raise ValueError("Cash bracket requires a long quantity and finite positive prices")
        if validate_prices and not stop_loss < reference < take_profit:
            raise ValueError("Long bracket requires stop_loss < entry price < take_profit")
        immediate = self.immediate_fill
        self.immediate_fill = False
        try:
            entry = self.submit_order(
                asset, quantity, order_type=entry_type, limit_price=entry_limit
            )
        finally:
            self.immediate_fill = immediate
        if entry is None or entry.status is OrderStatus.REJECTED:
            return None
        children = []
        for order_type, limit, stop, reason in (
            (OrderType.LIMIT, take_profit, None, "bracket_take_profit"),
            (OrderType.STOP, None, stop_loss, "bracket_stop_loss"),
        ):
            self._order_state.counter += 1
            child = Order(
                asset=asset,
                side=OrderSide.SELL,
                quantity=entry.quantity,
                order_type=order_type,
                limit_price=limit,
                stop_price=stop,
                parent_id=entry.order_id,
                order_id=f"ORD-{self._order_state.counter}",
                created_at=self._market_state.time,
                _created_bar_index=self._market_state.bar_index,
                _risk_exit_reason=reason,
            )
            self.constraint_kinds[child.order_id] = Kind.RISK
            self.order_requested[child.order_id] = child.quantity
            self.order_permitted[child.order_id] = 0.0
            self._order_state.orders.append(child)
            self.audit_order_event(child, "order", Decision(Action.DEFER, "bracket_wait_parent"))
            children.append(child)
        tp, sl = children
        self.bracket_children[entry.order_id] = (tp.order_id, sl.order_id)
        if (
            immediate
            and self.execution_mode is ExecutionMode.SAME_BAR
            and entry_type is OrderType.MARKET
        ):
            self._order_book._fill_immediately(entry)
            if entry.status is not OrderStatus.PENDING and entry in self._order_state.pending:
                self._order_state.pending.remove(entry)
        self.refresh_brackets()
        return entry, tp, sl

    def refresh_brackets(self) -> None:
        """Use canonical filled quantities, not a second position ledger."""
        for parent_id, child_ids in self.bracket_children.items():
            parent = self.get_order(parent_id)
            children = [self.get_order(child_id) for child_id in child_ids]
            assert parent is not None and all(child is not None for child in children)
            tp, sl = children
            assert tp is not None and sl is not None
            exited = tp.filled_quantity + sl.filled_quantity
            if exited and parent.status is OrderStatus.PENDING:
                self.cancel_constraint_order(parent_id, "bracket_exit_canceled_parent_remainder")
            exposure = max(0.0, parent.filled_quantity - exited)
            if exposure <= 1e-10:
                if parent.status is not OrderStatus.PENDING:
                    for child in (tp, sl):
                        if child.status is OrderStatus.PENDING:
                            child.status = OrderStatus.CANCELLED
                            self.audit_order_event(
                                child,
                                "terminal",
                                Decision(Action.ALLOW, "bracket_exposure_closed"),
                                status="canceled",
                            )
                            self.order_audit_state[child.order_id] = (
                                child.filled_quantity,
                                child.status,
                            )
                        if child in self._order_state.pending:
                            self._order_state.pending.remove(child)
                continue
            for child in (tp, sl):
                if child.status is OrderStatus.REJECTED:
                    raise RuntimeError(
                        f"Bracket protection failed: {child.order_id}: {child.rejection_reason}"
                    )
                if child.status is OrderStatus.PENDING:
                    child.quantity = exposure
                    self.order_permitted[child.order_id] = child.filled_quantity + exposure
                    if child.order_id in self._order_state.partial_quantities:
                        self._order_state.partial_quantities[child.order_id] = exposure
                    if child not in self._order_state.pending:
                        self._order_state.pending.append(child)

    def cancel_order(self, order_id):
        order = self.get_order(order_id)
        if order and order.parent_id in self.bracket_children:
            parent = self.get_order(order.parent_id)
            assert parent is not None
            if parent.filled_quantity > 0 and self.account.get_position_quantity(order.asset) > 0:
                return False  # do not leave a live bracket without its promised protection
        if order and order.rebalance_id in self.plan_manager.records:
            return self.plan_manager.cancel(order.rebalance_id)
        result = self.cancel_constraint_order(order_id, "order_canceled")
        self.refresh_brackets()
        return result

    def intent_for(self, order: Order, quantity: float | None = None) -> Intent:
        signed = (quantity if quantity is not None else order.quantity) * (
            1 if order.side is OrderSide.BUY else -1
        )
        kind = self.constraint_kinds.get(order.order_id)
        if kind is None:
            kind = (
                Kind.RISK
                if order._risk_exit_reason
                else Kind.REDUCE
                if signed < 0
                else Kind.ADD
                if order.asset in self.account.positions
                else Kind.ENTRY
            )
        return Intent(
            order.asset,
            signed,
            order.order_id,
            kind,
            order.order_type.value,
            self.constraint_targets.get(order.order_id),
            order.rebalance_id if order.rebalance_id in self.plan_manager.records else None,
        )

    def submit_intent(self, intent: Intent) -> Order | None:
        options = SubmitOrderOptions(
            risk_exit_reason="external_risk" if intent.kind is Kind.RISK else None,
            rebalance_id=intent.rebalance_id,
        )
        return self.submit_order(
            intent.asset,
            intent.quantity,
            order_type=OrderType(intent.order_type),
            _options=options,
            constraint_kind=intent.kind,
            target_weight=intent.target_weight,
            valid_until=intent.valid_until,
            time_in_force=intent.time_in_force,
        )

    def order_target_percent(
        self,
        asset,
        target_percent,
        order_type=OrderType.MARKET,
        limit_price=None,
        *,
        rebalance_id=None,
        valid_until=None,
        time_in_force=None,
    ):
        snapshot = self.constraint_state()
        price = self._market_state.prices.get(asset)
        if price is None or price <= 0:
            raise ValueError(f"Observed target price missing for {asset}")
        current = snapshot.holdings.get(asset)
        shares = current.quantity if current else 0
        shares += snapshot.pending_buys.get(asset, 0) / price - snapshot.pending_sells.get(asset, 0)
        if rebalance_id is not None:
            plan = self.plan_manager.records.get(rebalance_id)
            if plan is None or not plan.active or plan.targets.get(asset) != target_percent:
                self.audit_plan_rejection("rebalance_plan_not_active")
                return None
            pending = next(
                (
                    order
                    for identifier in plan.order_ids
                    if (order := self.get_order(identifier)) is not None
                    and order.asset == asset
                    and order.status is OrderStatus.PENDING
                ),
                None,
            )
            if pending is not None:
                return pending
            if asset in plan.completed_assets:
                return None
        return self.submit_order(
            asset,
            target_percent * snapshot.equity / price - shares,
            order_type=order_type,
            limit_price=limit_price,
            constraint_kind=Kind.REBALANCE,
            target_weight=target_percent,
            _options=SubmitOrderOptions(rebalance_id=rebalance_id),
            valid_until=valid_until,
            time_in_force=time_in_force,
        )

    def rebalance_to_weights(
        self,
        target_weights,
        order_type=OrderType.MARKET,
        *,
        rebalance_id=None,
        valid_until=None,
        defensive_allocation=False,
    ):
        if self.controller.config.rebalance_plan_enabled or rebalance_id is not None:
            if order_type is not OrderType.MARKET:
                raise ValueError("Continuation plans require market orders at observed prices")
            plan = self.create_rebalance_plan(
                target_weights,
                rebalance_id=rebalance_id,
                valid_until=valid_until,
                defensive_allocation=defensive_allocation,
            )
            return [self.get_order(identifier) for identifier in plan.order_ids] if plan else []
        timestamp = self._market_state.time
        assert timestamp is not None
        active = {asset: weight for asset, weight in target_weights.items() if weight != 0}
        sectors = {
            asset: self.context_provider(timestamp, asset, "submission").sector for asset in active
        }
        sectors = {asset: sector for asset, sector in sectors.items() if sector is not None}
        decision = self.controller.check_targets(
            active, sectors, defensive_allocation=defensive_allocation
        )
        if decision.action is not Action.ALLOW:
            self.controller.audit.append(
                Audit(
                    timestamp,
                    "portfolio",
                    "targets",
                    Kind.REBALANCE.value,
                    order_type.value,
                    "submission",
                    self.settled_cash,
                    0,
                    decision,
                )
            )
            return []
        targets = dict.fromkeys(self.account.positions, 0.0)
        targets.update(target_weights)
        snapshot = self.constraint_state()

        def delta(item):
            asset, weight = item
            held = snapshot.holdings.get(asset)
            current = held.quantity * held.price if held else 0
            return weight * snapshot.equity - current

        orders = []
        for asset, weight in sorted(targets.items(), key=delta):
            order = self.order_target_percent(asset, weight, order_type)
            if order is not None:
                orders.append(order)
        return orders

    def _process_orders(self, *args, **kwargs):
        self.expire_orders()
        self.plan_manager.advance()
        # Risk sells precede ordinary sells; the core exit_first mode then precedes buys.
        self._order_state.pending.sort(
            key=lambda order: 0 if self.intent_for(order).kind is Kind.RISK else 1
        )
        pending = tuple(self._order_state.pending)
        for order in pending:
            if (
                order.asset not in self._market_state.opens
                and order.asset not in self._market_state.prices
            ):
                self.audit_deferred(order, "price_missing")
        audit_start = len(self.controller.audit)
        result = super()._process_orders(*args, **kwargs)
        self.refresh_brackets()
        checked = {
            record.order_id
            for record in self.controller.audit[audit_start:]
            if record.phase in {"fill", "fill_precheck"}
        }
        use_open = kwargs.get("use_open", args[0] if args else False)
        for order in pending:
            if order.status is OrderStatus.REJECTED and order.order_id not in checked:
                price = self._fill_engine.get_fill_price_for_order(order, use_open)
                self.audit_core_rejection(order, price or 0, "fill_precheck")
            self.audit_order_transition(order)
        self.plan_manager.advance()
        return result

    def audit_core_rejection(self, order: Order, price: float, phase: str) -> None:
        """Capture upstream prechecks that terminate before the actual fill callback."""
        timestamp = self._market_state.time
        assert timestamp is not None
        intent = self.intent_for(order)
        assert self._cash_account_rules is not None
        available = self.settled_cash - self._cash_account_rules.reserved_cash(order.order_id)
        required = max(
            0,
            intent.quantity * price
            + calculate_commission(self.commission_model, order.asset, abs(intent.quantity), price),
        )
        self.controller.audit.append(
            Audit(
                timestamp,
                order.asset,
                order.order_id,
                intent.kind.value,
                intent.order_type,
                phase,
                available,
                required,
                Decision(
                    Action.REJECT,
                    order._rejection_code or "core_rejection",
                    order.rejection_reason or "",
                    data={"funds_basis": "precheck_estimate"},
                ),
                side=order.side.value,
                requested_quantity=self.order_requested.get(order.order_id, abs(intent.quantity)),
                final_status=order.status.value,
            )
        )

    def evaluate_position_rules(self):
        orders = super().evaluate_position_rules()
        timestamp = self._market_state.time
        assert timestamp is not None
        bounds = self.controller.calendar.bounds(timestamp)
        if bounds is None or not bounds[0] <= timestamp < bounds[1]:
            self.deferred_rule_assets.update(self._risk_state.pending_exits)
        return orders

    def _process_pending_exits(self):
        for asset in self.deferred_rule_assets:
            if asset in self._risk_state.pending_exits:
                self._risk_state.pending_exits[asset]["fill_price"] = None
        orders = super()._process_pending_exits()
        for order in orders:
            if order.asset in self.deferred_rule_assets:
                self.deferred_risk.add(order.order_id)
        self.deferred_rule_assets.difference_update(order.asset for order in orders)
        return orders

    def submit_order(
        self,
        asset,
        quantity,
        side=None,
        order_type=OrderType.MARKET,
        limit_price=None,
        stop_price=None,
        trail_amount=None,
        _options=None,
        *,
        constraint_kind=None,
        target_weight=None,
        valid_until=None,
        time_in_force=None,
    ):
        if not hasattr(self, "controller"):
            return super().submit_order(
                asset, quantity, side, order_type, limit_price, stop_price, trail_amount, _options
            )
        if quantity == 0:
            return None
        signed = quantity if side is None else abs(quantity) * (1 if side is OrderSide.BUY else -1)
        kind = constraint_kind or (
            Kind.RISK
            if _options and _options.risk_exit_reason
            else Kind.REBALANCE
            if _options and (_options.rebalance_id or _options.target_intent_id)
            else Kind.REDUCE
            if signed < 0
            else Kind.ADD
            if asset in self.account.positions
            else Kind.ENTRY
        )
        if kind is Kind.RISK:
            for pending in self._order_state.pending:
                if (
                    pending.asset == asset
                    and pending.status is OrderStatus.PENDING
                    and self.intent_for(pending).kind is Kind.RISK
                    and pending.parent_id is None
                    and pending.order_type is OrderType.MARKET
                    and signed < 0
                    and -signed <= self.account.get_position_quantity(asset)
                ):
                    return pending
        phase = "fill" if self.risk_monitor_submission else "submission"
        marks = (
            self._market_state.opens if self.risk_monitor_submission else self._market_state.prices
        )
        price = limit_price or max(marks.get(asset, 0), stop_price or 0)
        timestamp = self._market_state.time
        assert timestamp is not None
        if self.constraint_anchor is None:
            self.constraint_anchor = timestamp
        next_id = f"ORD-{self._order_state.counter + 1}"
        rebalance_id = (
            _options.rebalance_id
            if _options and _options.rebalance_id in self.plan_manager.records
            else None
        )
        intent = Intent(asset, signed, next_id, kind, order_type.value, target_weight, rebalance_id)
        deadline = self.order_deadline(kind, time_in_force, valid_until, rebalance_id, asset)
        reference_price = price
        price = self.estimate_execution_price(asset, signed, price)
        commission = self.estimate_order_fees(asset, signed, price, next_id)
        was_new = asset not in self.account.positions
        snapshot = self.constraint_state(phase=phase)
        if kind is Kind.RISK:
            # Validate before superseding ordinary reductions: rejects are atomic.
            other_risk_sells = sum(
                o.quantity
                for o in self._order_state.pending
                if o.asset == asset
                and o.side is OrderSide.SELL
                and self.intent_for(o).kind is Kind.RISK
                and o.parent_id is None
            )
            snapshot = replace(
                snapshot, pending_sells={**snapshot.pending_sells, asset: other_risk_sells}
            )
        decision = self.controller.check(
            intent,
            snapshot,
            self.context_for(asset, price, "submission", commission, reference_price),
        )
        if (
            kind is not Kind.RISK
            and any(plan.active for plan in self.plan_manager.records.values())
            and rebalance_id is None
        ):
            decision = Decision(Action.REJECT, "rebalance_plan_order_conflict")
        if rebalance_id is not None:
            plan = self.plan_manager.records[rebalance_id]
            price_now = self._market_state.prices.get(asset, 0)
            expected = (
                plan.targets.get(asset, 0) * snapshot.equity / price_now
                - self.account.get_position_quantity(asset)
                if price_now > 0
                else 0
            )
            if (
                not plan.active
                or order_type is not OrderType.MARKET
                or asset not in plan.targets
                or target_weight != plan.targets[asset]
                or signed * expected <= 0
                or abs(signed) > abs(expected) + 1e-8
                or (signed > 0 and plan.status == "selling")
                or snapshot.pending_buys.get(asset, 0)
                or snapshot.pending_sells.get(asset, 0)
            ):
                decision = Decision(Action.REJECT, "rebalance_plan_order_conflict")
            elif decision.action is Action.RESIZE:
                decision = Decision(Action.REJECT, "rebalance_plan_target_illegal")
        self.order_requested[next_id] = abs(quantity)
        if self.controller.audit[-1].decision != decision:
            self.controller.audit[-1] = replace(
                self.controller.audit[-1], decision=decision, permitted_quantity=0.0
            )
        if decision.action is Action.REJECT:
            self._order_state.counter += 1
            order = Order(
                asset=asset,
                side=OrderSide.BUY if signed > 0 else OrderSide.SELL,
                quantity=abs(signed),
                order_type=order_type,
                order_id=next_id,
                created_at=timestamp,
            )
            order.reject(decision.reason or decision.code, decision.code)
            self._order_state.orders.append(order)
            self.constraint_kinds[next_id] = kind
            self.audit_order_event(order, "order", decision)
            self.audit_order_transition(order)
            return order
        if decision.action is Action.RESIZE:
            assert decision.quantity is not None
            signed = decision.quantity
        if kind is Kind.RISK:
            for identifier, plan in tuple(self.plan_manager.records.items()):
                if plan.active:
                    self.plan_manager.cancel(identifier, "rebalance_plan_superseded_by_risk")
            for pending in tuple(self._order_state.pending):
                if pending.asset == asset and pending.side is OrderSide.SELL:
                    if pending.parent_id in self.bracket_children:
                        self.cancel_constraint_order(pending.parent_id, "risk_superseded")
                    self.cancel_constraint_order(pending.order_id, "risk_superseded")
        # Register before core reserve/fill callbacks so final checks retain intent kind.
        self.constraint_kinds[next_id] = kind
        self.order_permitted[next_id] = 0.0 if decision.action is Action.DEFER else abs(signed)
        if deadline is not None:
            self.order_validity[next_id] = deadline
        if target_weight is not None:
            self.constraint_targets[next_id] = target_weight
        order = super().submit_order(
            asset, signed, None, order_type, limit_price, stop_price, trail_amount, _options
        )
        if order is not None and rebalance_id is not None:
            plan = self.plan_manager.records[rebalance_id]
            if order.order_id not in plan.order_ids:
                self.plan_manager.records[rebalance_id] = replace(
                    plan, order_ids=(*plan.order_ids, order.order_id)
                )
        if order is not None:
            self.audit_order_event(order, "order", decision)
            self.audit_order_transition(order)
        if order is not None and order.status is OrderStatus.REJECTED:
            self.audit_core_rejection(order, price, "reservation")
        if order is not None and order.status is not OrderStatus.REJECTED:
            if kind is not Kind.RISK and signed > 0 and was_new:
                self.entry_times.append(timestamp)
            if decision.action is Action.DEFER:
                if kind is Kind.RISK:
                    self.deferred_risk.add(order.order_id)
                self.audit_deferred(order, decision.code)
        return order

    def validate_cash_fill(self, order, quantity, price, commission):
        if not hasattr(self, "controller"):
            return super().validate_cash_fill(order, quantity, price, commission)
        # Final execution is separate from conservative reservation estimates.
        phase = "fill" if getattr(self, "checking_actual_fill", False) else "reservation"
        context = self.context_for(order.asset, price, phase, commission)
        # Reservation repeats only hard account checks; the submission gates already ran.
        state = self.constraint_state(order.order_id, "fill" if phase == "fill" else "submission")
        intent = self.intent_for(order, quantity)
        if phase == "fill":
            decision = self.controller.check(intent, state, context)
            if order.rebalance_id in self.plan_manager.records:
                plan = self.plan_manager.records[order.rebalance_id]
                if (
                    abs(price / plan.reference_prices[order.asset] - 1)
                    > self.controller.config.rebalance_plan_max_price_change + 1e-12
                ):
                    check_record = self.controller.audit[-1]
                    self.plan_manager.cancel(plan.plan_id, "rebalance_plan_price_changed")
                    decision = Decision(Action.REJECT, "rebalance_plan_price_changed")
                    self.controller.audit.append(
                        replace(check_record, decision=decision, permitted_quantity=0.0)
                    )
        else:
            decision = self.controller.account.check(intent, state, context)
        if decision.action not in {Action.ALLOW}:
            self.fill_rejections[order.order_id] = decision.code
            return False, decision.reason or decision.code
        if phase == "fill":
            self.order_permitted[order.order_id] = max(
                self.order_permitted.get(order.order_id, 0), order.filled_quantity + quantity
            )
        return super().validate_cash_fill(order, quantity, price, commission)

    def update_order(self, order_id, **kwargs):
        order = next((o for o in self._order_state.pending if o.order_id == order_id), None)
        if order is None:
            return False
        # Price-only amendments preserve the real unfilled quantity. The core
        # cash amendment API treats quantity as the replacement's remainder.
        # Otherwise a partial order's original quantity would be replenished.
        remainder = self._order_state.partial_quantities.get(order_id, order.quantity)
        if set(kwargs) - self._order_book._UPDATABLE_ORDER_FIELDS:
            return super().update_order(order_id, **kwargs)  # retain core validation/error
        if all(
            (remainder if name == "quantity" else getattr(order, name)) == value
            for name, value in kwargs.items()
        ):
            return True
        kwargs.setdefault("quantity", remainder)
        candidate = replace(order, **kwargs)
        price = candidate.limit_price or max(
            self._market_state.prices.get(candidate.asset, 0), candidate.stop_price or 0
        )
        reference_price = price
        price = self.estimate_execution_price(
            candidate.asset,
            candidate.quantity * (1 if candidate.side is OrderSide.BUY else -1),
            price,
        )
        context = self.context_for(
            candidate.asset,
            price,
            "amendment",
            self.estimate_order_fees(
                candidate.asset,
                candidate.quantity * (1 if candidate.side is OrderSide.BUY else -1),
                price,
                order_id,
            ),
            reference_price,
        )
        decision = self.controller.check(
            self.intent_for(candidate), self.constraint_state(order_id), context
        )
        if decision.action is not Action.ALLOW:
            return False
        bridge = self.fee_bridge
        if bridge is None:
            return super().update_order(order_id, **kwargs)
        generation = bridge.generations.get(order_id, 0) + 1
        # Core reserve_cash_order binds the candidate, so make the tentative
        # generation visible only for the duration of this atomic amendment.
        old_generation = bridge.generations.get(order_id, 0)
        bridge.generations[order_id] = generation
        try:
            changed = super().update_order(order_id, **kwargs)
        except Exception:
            bridge.generations[order_id] = old_generation
            raise
        if not changed:
            bridge.generations[order_id] = old_generation
        return changed

    def settle_cash_fill(self, order, remaining_quantity, cash_change):
        assert self._cash_account_rules is not None
        if self.fee_bridge is not None:
            self.fee_bridge.commit_fill(order, self._execution_journal.fills[-1])
        if self.regime_slippage is not None:
            self.regime_slippage.commit_fill(order, self._execution_journal.fills[-1])
        with self.fee_bridge.bind(order) if self.fee_bridge is not None else nullcontext():
            self._cash_account_rules.reserve_remainder(order, remaining_quantity)
        if order.side is OrderSide.SELL and cash_change > 0:
            timestamp = self._market_state.time
            assert timestamp is not None
            due = settlement_date(
                timestamp.astimezone(NY).date(),
                self.controller.config.settlement_cycle,
                self._cash_account_rules.extra_holidays,
            )
            self.account.add_dated_settlement_hold(due, cash_change)

    def _update_time(self, timestamp, prices, opens, highs=None, lows=None, *rest, **kwargs):
        aware(timestamp)
        processor = getattr(self, "corporate_action_processor", None)
        if processor is None:
            super()._update_time(timestamp, prices, opens, highs, lows, *rest, **kwargs)
        else:
            snapshot = self._snapshot_lifecycle_state(
                all_positions=True, all_pending_orders=True, risk_rules=True, all_asset_stats=True
            )
            try:
                observed = set(prices) | set(opens)
                prepared = processor.prepare(timestamp, observed)
                super()._update_time(timestamp, prices, opens, highs, lows, *rest, **kwargs)
                processor.apply(timestamp, observed, prepared)
            except Exception as error:
                self._restore_lifecycle_state(snapshot)
                processor.failures.append(
                    {"timestamp": timestamp.isoformat(), "reason": str(error)}
                )
                raise
        if hasattr(self, "controller"):
            self.expire_orders()
        if (
            hasattr(self, "controller")
            and self.constraint_anchor is None
            and self.controller.calendar.bounds(timestamp)
        ):
            self.constraint_anchor = timestamp
        if not hasattr(self, "controller") or not self.account.positions:
            return
        state = self.constraint_state(phase="fill")
        asset = next(iter(state.holdings))
        context = self.context_for(asset, state.holdings[asset].price, "monitor")
        if state.drawdown < self.controller.config.drawdown_reduce:
            self.drawdown_plan_applied = False
        applied = self.drawdown_plan_applied
        self.risk_monitor_submission = True
        try:
            for request in self.controller.risk_requests(state, context):
                if request.reason == "drawdown_predefined_reduction" and applied:
                    continue
                order = self.submit_order(
                    request.asset,
                    -request.quantity,
                    _options=SubmitOrderOptions(
                        eligible_in_next_bar_mode=True, risk_exit_reason=request.reason
                    ),
                )
                if order and order.status is OrderStatus.PENDING:
                    if (
                        request.reason == "overweight_predefined_reduction"
                        and self.controller.config.drift_reduction == "next_session"
                    ):
                        self.deferred_until.setdefault(
                            order.order_id,
                            self.controller.calendar.shift(timestamp.astimezone(NY).date(), 1),
                        )
                        self.deferred_risk.add(order.order_id)
                    if request.reason == "drawdown_predefined_reduction":
                        self.drawdown_plan_applied = True
        finally:
            self.risk_monitor_submission = False

    def corporate_action_evidence(self):
        processor = getattr(self, "corporate_action_processor", None)
        return processor.evidence() if processor is not None else None

    def _snapshot_lifecycle_state(self, **scope):
        snapshot = super()._snapshot_lifecycle_state(**scope)
        if getattr(self, "corporate_action_processor", None) is not None:
            snapshot["corporate_extension"] = {
                "market": copy.deepcopy(self._market_state),
                "orders": [(order, copy.deepcopy(vars(order))) for order in self.orders],
                "maps": {
                    name: copy.deepcopy(getattr(self, name))
                    for name in ("order_requested", "order_permitted", "order_audit_state")
                },
                "plans": dict(self.plan_manager.records),
                "audit_length": len(self.controller.audit),
            }
        return snapshot

    def _restore_lifecycle_state(self, state):
        super()._restore_lifecycle_state(state)
        extra = state.get("corporate_extension")
        if extra is not None:
            for item in fields(self._market_state):
                setattr(self._market_state, item.name, getattr(extra["market"], item.name))
            for order, state in extra["orders"]:
                order.__dict__.clear()
                order.__dict__.update(state)
            for name, mapping in extra["maps"].items():
                setattr(self, name, mapping)
            self.plan_manager.records = extra["plans"]
            del self.controller.audit[extra["audit_length"] :]


def broker_factory(
    controller: ConstraintController,
    context_provider: ContextProvider,
    *,
    fee_model: IBKRProTieredUSStock | None = None,
    corporate_action_provider: CorporateActionProvider | None = None,
):
    def factory(config: BacktestConfig, **kwargs) -> ConstrainedBroker:
        broker = ConstrainedBroker.from_config(config, **kwargs)
        assert isinstance(broker, ConstrainedBroker)
        broker.configure_constraints(
            controller, context_provider, config, fee_model, corporate_action_provider
        )
        return broker

    return factory


def constrained_engine(
    feed,
    strategy,
    controller: ConstraintController,
    context_provider: ContextProvider,
    config: BacktestConfig | None = None,
    *,
    corporate_action_provider: CorporateActionProvider | None = None,
    **kwargs,
) -> Engine:
    return Engine(
        feed,
        strategy,
        config or cash_backtest_config(),
        broker_factory=broker_factory(
            controller, context_provider, corporate_action_provider=corporate_action_provider
        ),
        **kwargs,
    )
