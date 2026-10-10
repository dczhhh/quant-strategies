"""Opt-in backtest adapter; core cash, position and order state remain canonical."""

import math
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from ml4t.backtest import BacktestConfig, Broker, Engine
from ml4t.backtest.config import DataFrequency, ExecutionPrice, FillOrdering
from ml4t.backtest.core.shared import SubmitOrderOptions
from ml4t.backtest.execution.fill_executor import FillExecutor
from ml4t.backtest.models import calculate_commission
from ml4t.backtest.types import ExecutionMode, Order, OrderSide, OrderStatus, OrderType

from .calendar import NY, settlement_date
from .controller import ConstraintController
from .models import Action, Audit, Decision, Holding, Intent, Kind, MarketContext, State, aware

ContextProvider = Callable[[datetime, str, str], MarketContext]


def cash_backtest_config(**changes) -> BacktestConfig:
    config = BacktestConfig.from_preset("us_cash_equities")
    defaults = {
        "data_frequency": DataFrequency.MINUTE_1,
        "execution_mode": ExecutionMode.NEXT_BAR,
        "execution_price": ExecutionPrice.OPEN,
        "cash_buffer_pct": 0.0,
        "fill_ordering": FillOrdering.EXIT_FIRST,
    }
    defaults.update(changes)
    return replace(config, **defaults)


class ConstraintFillExecutor(FillExecutor):
    def execute(self, order: Order, base_price: float) -> bool:
        broker = self.broker
        assert isinstance(broker, ConstrainedBroker)
        intent = broker.intent_for(order)
        context = broker.context_for(order.asset, base_price, "fill")
        state = broker.constraint_state(order.order_id, phase="fill")
        if order.asset not in self.market.opens and order.asset not in self.market.prices:
            return False
        due = broker.deferred_until.get(order.order_id)
        if due and context.asof.astimezone(NY).date() < due:
            return False
        session = broker.controller.session.check(intent, state, context)
        if session.action is not Action.ALLOW:
            broker.controller.check(intent, state, context)
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
            return False
        broker.checking_actual_fill = True
        try:
            completed = super().execute(order, base_price)
        finally:
            broker.checking_actual_fill = False
        broker.refresh_brackets()
        if order.status is OrderStatus.REJECTED and order.order_id in broker.fill_rejections:
            order._rejection_code = broker.fill_rejections.pop(order.order_id)
        return completed


class ConstrainedBroker(Broker):
    """Configure via broker_factory; no changes to unconfigured upstream Broker."""

    def configure_constraints(
        self,
        controller: ConstraintController,
        context_provider: ContextProvider,
        config: BacktestConfig,
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
        self.controller = controller
        self.context_provider = context_provider
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

    def context_for(
        self, asset: str, price: float, phase: str, commission: float = 0
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
            sector = self.context_provider(market.time, asset, phase).sector
            holdings[asset] = Holding(position.quantity, price, sector, position.entry_time)
        equity = self.cash + sum(h.quantity * h.price for h in holdings.values())
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
        )

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
            self._order_state.orders.append(child)
            children.append(child)
        tp, sl = children
        self.bracket_children[entry.order_id] = (tp.order_id, sl.order_id)
        if (
            immediate
            and self.execution_mode is ExecutionMode.SAME_BAR
            and entry_type is OrderType.MARKET
        ):
            self._order_book._fill_immediately(entry)
            if entry in self._order_state.pending:
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
                super().cancel_order(parent_id)
            exposure = max(0.0, parent.filled_quantity - exited)
            if exposure <= 1e-10:
                if parent.status is not OrderStatus.PENDING:
                    for child in (tp, sl):
                        if child.status is OrderStatus.PENDING:
                            child.status = OrderStatus.CANCELLED
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
        result = super().cancel_order(order_id)
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
        )

    def submit_intent(self, intent: Intent) -> Order | None:
        options = SubmitOrderOptions(
            risk_exit_reason="external_risk" if intent.kind is Kind.RISK else None
        )
        return self.submit_order(
            intent.asset,
            intent.quantity,
            order_type=OrderType(intent.order_type),
            _options=options,
            constraint_kind=intent.kind,
            target_weight=intent.target_weight,
        )

    def order_target_percent(
        self, asset, target_percent, order_type=OrderType.MARKET, limit_price=None
    ):
        snapshot = self.constraint_state()
        price = self._market_state.prices.get(asset)
        if price is None or price <= 0:
            raise ValueError(f"Observed target price missing for {asset}")
        current = snapshot.holdings.get(asset)
        shares = current.quantity if current else 0
        shares += snapshot.pending_buys.get(asset, 0) / price - snapshot.pending_sells.get(asset, 0)
        return self.submit_order(
            asset,
            target_percent * snapshot.equity / price - shares,
            order_type=order_type,
            limit_price=limit_price,
            constraint_kind=Kind.REBALANCE,
            target_weight=target_percent,
        )

    def rebalance_to_weights(self, target_weights, order_type=OrderType.MARKET):
        timestamp = self._market_state.time
        assert timestamp is not None
        active = {asset: weight for asset, weight in target_weights.items() if weight != 0}
        sectors = {
            asset: self.context_provider(timestamp, asset, "submission").sector for asset in active
        }
        sectors = {asset: sector for asset, sector in sectors.items() if sector is not None}
        decision = self.controller.check_targets(active, sectors)
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
        # Risk sells precede ordinary sells; the core exit_first mode then precedes buys.
        self._order_state.pending.sort(
            key=lambda order: 0 if self.intent_for(order).kind is Kind.RISK else 1
        )
        pending = tuple(self._order_state.pending)
        audit_start = len(self.controller.audit)
        result = super()._process_orders(*args, **kwargs)
        self.refresh_brackets()
        checked = {record.order_id for record in self.controller.audit[audit_start:]}
        use_open = kwargs.get("use_open", args[0] if args else False)
        for order in pending:
            if order.status is OrderStatus.REJECTED and order.order_id not in checked:
                price = self._fill_engine.get_fill_price_for_order(order, use_open)
                self.audit_core_rejection(order, price or 0, "fill_precheck")
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
        intent = Intent(asset, signed, next_id, kind, order_type.value, target_weight)
        commission = calculate_commission(self.commission_model, asset, abs(signed), price)
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
            self.context_for(asset, price, "submission", commission),
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
            return order
        if decision.action is Action.RESIZE:
            assert decision.quantity is not None
            signed = decision.quantity
        if kind is Kind.RISK:
            for pending in tuple(self._order_state.pending):
                if pending.asset == asset and pending.side is OrderSide.SELL:
                    if pending.parent_id in self.bracket_children:
                        super().cancel_order(pending.parent_id)
                    super().cancel_order(pending.order_id)
        # Register before core reserve/fill callbacks so final checks retain intent kind.
        self.constraint_kinds[next_id] = kind
        if target_weight is not None:
            self.constraint_targets[next_id] = target_weight
        order = super().submit_order(
            asset, signed, None, order_type, limit_price, stop_price, trail_amount, _options
        )
        if order is not None and order.status is OrderStatus.REJECTED:
            self.audit_core_rejection(order, price, "reservation")
        if order is not None and order.status is not OrderStatus.REJECTED:
            if kind is not Kind.RISK and signed > 0 and was_new:
                self.entry_times.append(timestamp)
            if decision.action is Action.DEFER:
                self.deferred_risk.add(order.order_id)
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
        else:
            decision = self.controller.account.check(intent, state, context)
        if decision.action not in {Action.ALLOW}:
            self.fill_rejections[order.order_id] = decision.code
            return False, decision.reason or decision.code
        return super().validate_cash_fill(order, quantity, price, commission)

    def update_order(self, order_id, **kwargs):
        order = next((o for o in self._order_state.pending if o.order_id == order_id), None)
        if order is None:
            return False
        candidate = replace(order, **kwargs)
        price = candidate.limit_price or max(
            self._market_state.prices.get(candidate.asset, 0), candidate.stop_price or 0
        )
        context = self.context_for(
            candidate.asset,
            price,
            "amendment",
            calculate_commission(self.commission_model, candidate.asset, candidate.quantity, price),
        )
        decision = self.controller.check(
            self.intent_for(candidate), self.constraint_state(order_id), context
        )
        if decision.action is not Action.ALLOW:
            return False
        return super().update_order(order_id, **kwargs)

    def settle_cash_fill(self, order, remaining_quantity, cash_change):
        assert self._cash_account_rules is not None
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
        super()._update_time(timestamp, prices, opens, highs, lows, *rest, **kwargs)
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


def broker_factory(controller: ConstraintController, context_provider: ContextProvider):
    def factory(config: BacktestConfig, **kwargs) -> ConstrainedBroker:
        broker = ConstrainedBroker.from_config(config, **kwargs)
        assert isinstance(broker, ConstrainedBroker)
        broker.configure_constraints(controller, context_provider, config)
        return broker

    return factory


def constrained_engine(
    feed,
    strategy,
    controller: ConstraintController,
    context_provider: ContextProvider,
    config: BacktestConfig | None = None,
    **kwargs,
) -> Engine:
    return Engine(
        feed,
        strategy,
        config or cash_backtest_config(),
        broker_factory=broker_factory(controller, context_provider),
        **kwargs,
    )
