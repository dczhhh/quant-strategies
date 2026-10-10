"""Continuation of an external allocation, using only the broker's canonical orders.

Plan records carry authorization and progress, never a second cash/position ledger.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING

from ml4t.backtest.types import OrderSide, OrderStatus

from .calendar import NY
from .models import Action, Audit, Decision, Intent, Kind, aware

if TYPE_CHECKING:
    from .adapter import ConstrainedBroker


@dataclass(frozen=True)
class RebalancePlan:
    plan_id: str
    targets: Mapping[str, float]
    created_at: datetime
    valid_until: datetime
    reference_prices: Mapping[str, float]
    defensive_allocation: bool = False
    status: str = "selling"
    reason: str = ""
    order_ids: tuple[str, ...] = ()
    completed_assets: frozenset[str] = frozenset()

    @property
    def active(self) -> bool:
        return self.status in {"selling", "waiting_cash", "buying"}


class RebalancePlanManager:
    def __init__(self, broker: "ConstrainedBroker"):
        self.broker = broker
        self.records: dict[str, RebalancePlan] = {}
        self.counter = 0

    def audit(self, plan: RebalancePlan, action: Action, code: str) -> None:
        broker = self.broker
        assert broker._market_state.time is not None
        broker.controller.audit.append(
            Audit(
                broker._market_state.time,
                "portfolio",
                plan.plan_id,
                Kind.REBALANCE.value,
                "market",
                "plan",
                broker.settled_cash,
                0,
                Decision(
                    action,
                    code,
                    data={"created_at": plan.created_at, "valid_until": plan.valid_until},
                ),
                final_status=plan.status,
            )
        )

    def create(
        self,
        targets: dict[str, float],
        *,
        plan_id: str | None = None,
        valid_until: datetime | None = None,
        defensive_allocation: bool = False,
    ) -> RebalancePlan | None:
        broker, controller = self.broker, self.broker.controller
        now = broker._market_state.time
        assert now is not None
        if plan_id is not None and (not isinstance(plan_id, str) or not plan_id.strip()):
            raise ValueError("rebalance_id must be a nonempty string")
        existing = self.records.get(plan_id or "")
        if existing is not None:
            explicit = {asset: weight for asset, weight in existing.targets.items() if weight != 0}
            if (
                explicit != {asset: weight for asset, weight in targets.items() if weight != 0}
                or existing.defensive_allocation != defensive_allocation
            ):
                raise ValueError("Rebalance ID already identifies a different allocation")
            if valid_until is not None and valid_until != existing.valid_until:
                raise ValueError("An existing plan's validity cannot be extended")
            return existing  # no repeat orders and no resurrection of terminal plans
        bounds = controller.calendar.bounds(now)
        if bounds is None or not bounds[0] <= now < bounds[1]:
            broker.audit_plan_rejection("outside_rth")
            return None
        snapshot = broker.constraint_state()
        if not controller.rebalance.scheduled(snapshot, now):
            broker.audit_plan_rejection("rebalance_schedule")
            return None
        if any(plan.active for plan in self.records.values()) or broker.get_pending_orders():
            broker.audit_plan_rejection("rebalance_plan_order_conflict")
            return None
        if snapshot.missing_marks:
            broker.audit_plan_rejection("portfolio_marks_missing")
            return None
        active = {asset: weight for asset, weight in targets.items() if weight != 0}
        sectors = {
            asset: broker.context_provider(now, asset, "submission").sector for asset in active
        }
        decision = controller.check_targets(
            active,
            {a: s for a, s in sectors.items() if s is not None},
            defensive_allocation=defensive_allocation,
        )
        if decision.action is not Action.ALLOW:
            broker.audit_plan_rejection(decision.code)
            return None
        complete_targets = dict.fromkeys(snapshot.holdings, 0.0)
        complete_targets.update(targets)
        prices = {}
        for asset in complete_targets:
            price = broker._market_state.prices.get(asset)
            if price is None or not math.isfinite(price) or price <= 0:
                broker.audit_plan_rejection("rebalance_plan_price_missing")
                return None
            prices[asset] = price
            if active.get(asset, 0) and broker.context_for(
                asset, price, "plan_check"
            ).instrument not in {"equity", "plain_sector_etf"}:
                broker.audit_plan_rejection("instrument_not_allowed")
                return None
        last_day = controller.calendar.shift(
            now.astimezone(NY).date(), controller.config.rebalance_plan_sessions - 1
        )
        last_session = controller.calendar.session(last_day)
        assert last_session is not None
        expiry = last_session.market_close
        if valid_until is not None:
            expiry = min(expiry, aware(valid_until))
        if expiry <= now:
            raise ValueError("A rebalance plan must have future validity")
        self.counter += 1
        identifier = plan_id or f"REBALANCE-{self.counter}"
        while identifier in self.records:
            self.counter += 1
            identifier = f"REBALANCE-{self.counter}"
        completed = frozenset(
            asset
            for asset, weight in complete_targets.items()
            if (
                weight > 0
                and abs(
                    weight
                    - (
                        snapshot.holdings[asset].quantity * prices[asset] / snapshot.equity
                        if asset in snapshot.holdings
                        else 0
                    )
                )
                < controller.config.weight_change_threshold - 1e-12
            )
            or abs(
                weight * snapshot.equity / prices[asset]
                - broker.account.get_position_quantity(asset)
            )
            <= 1e-10
        )
        plan = RebalancePlan(
            identifier,
            MappingProxyType(complete_targets),
            now,
            expiry,
            MappingProxyType(prices),
            defensive_allocation,
            completed_assets=completed,
        )
        self.records[identifier] = plan
        self.audit(plan, Action.ALLOW, "rebalance_plan_created")
        self.advance()
        return self.records[identifier]

    def cancel(
        self, plan_id: str, reason: str = "rebalance_plan_canceled", *, expired: bool = False
    ) -> bool:
        plan = self.records.get(plan_id)
        if plan is None or not plan.active:
            return False
        # Close authorization first; cancellation must never restart a plan.
        plan = replace(plan, status="expired" if expired else "canceled", reason=reason)
        self.records[plan_id] = plan
        for order_id in plan.order_ids:
            self.broker.cancel_constraint_order(order_id, reason, expired=expired)
        self.audit(plan, Action.REJECT, reason)
        return True

    def authorized(self, now: datetime) -> frozenset[str]:
        return frozenset(
            p.plan_id for p in self.records.values() if p.active and now < p.valid_until
        )

    def advance(self) -> None:
        broker, controller = self.broker, self.broker.controller
        now = broker._market_state.time
        assert now is not None
        for plan_id in tuple(self.records):
            plan = self.records[plan_id]
            if not plan.active:
                continue
            if now >= plan.valid_until:
                self.cancel(plan_id, "rebalance_plan_expired", expired=True)
                continue
            orders = [broker.get_order(identifier) for identifier in plan.order_ids]
            assert all(order is not None for order in orders)
            completed = set(plan.completed_assets)
            failed = False
            for order in orders:
                assert order is not None
                if order.status in {OrderStatus.CANCELLED, OrderStatus.REJECTED}:
                    self.cancel(plan_id, "rebalance_plan_order_terminated")
                    failed = True
                    break
                if order.status is OrderStatus.FILLED:
                    completed.add(order.asset)
            if failed:
                continue
            plan = replace(plan, completed_assets=frozenset(completed))
            self.records[plan_id] = plan
            # A completed plan remains terminal even if subsequent prices move.
            if completed == set(plan.targets):
                plan = replace(plan, status="completed")
                self.records[plan_id] = plan
                self.audit(plan, Action.ALLOW, "rebalance_plan_completed")
                continue
            invalid = False
            for asset, reference in plan.reference_prices.items():
                current = broker._market_state.prices.get(asset)
                observations = (current, broker._market_state.opens.get(asset))
                if any(
                    value is not None
                    and (
                        not math.isfinite(value)
                        or value <= 0
                        or abs(value / reference - 1)
                        > controller.config.rebalance_plan_max_price_change + 1e-12
                    )
                    for value in observations
                ):
                    self.cancel(plan_id, "rebalance_plan_price_changed")
                    invalid = True
                    break
                if plan.targets[asset] > 0 and asset not in completed:
                    context = broker.context_for(asset, current or reference, "plan_check")
                    if context.instrument not in {"equity", "plain_sector_etf"}:
                        self.cancel(plan_id, "rebalance_plan_target_illegal")
                        invalid = True
                        break
                    # Cash starvation must not hide a newly published earnings blackout.
                    snapshot = broker.constraint_state()
                    holding = snapshot.holdings.get(asset)
                    if (
                        holding is not None
                        and plan.targets[asset] * snapshot.equity
                        <= holding.quantity * holding.price + 1e-10
                    ):
                        continue
                    earnings = controller.earnings.check(
                        Intent(asset, 1), broker.constraint_state(), context
                    )
                    _, market = controller.portfolio.cap(asset, broker.constraint_state(), context)
                    if earnings.action is Action.REJECT or market.action is Action.REJECT:
                        self.cancel(plan_id, "rebalance_plan_target_illegal")
                        invalid = True
                        break
            if invalid:
                continue
            bounds = controller.calendar.bounds(now)
            if bounds is None or not bounds[0] <= now < bounds[1]:
                continue
            snapshot = broker.constraint_state()
            if snapshot.missing_marks:
                continue
            active = {a: w for a, w in plan.targets.items() if w > 0}
            sectors = {a: broker.context_provider(now, a, "plan_check").sector for a in active}
            target_check = controller.check_targets(
                active,
                {a: s for a, s in sectors.items() if s is not None},
                defensive_allocation=plan.defensive_allocation,
            )
            if target_check.action is not Action.ALLOW:
                self.cancel(plan_id, "rebalance_plan_target_illegal")
                continue
            pending = {
                o.asset: o for o in orders if o is not None and o.status is OrderStatus.PENDING
            }
            deltas = {
                asset: weight * snapshot.equity / price
                - broker.account.get_position_quantity(asset)
                for asset, weight in plan.targets.items()
                if asset not in completed
                and asset not in pending
                and (price := broker._market_state.prices.get(asset)) is not None
                and price > 0
            }
            sells = {a: d for a, d in deltas.items() if d < -1e-10}
            # Missing unsold prices must not let the buy phase run ahead of its sells.
            unobserved = set(plan.targets) - completed - pending.keys() - deltas.keys()
            if sells or any(o.side is OrderSide.SELL for o in pending.values()) or unobserved:
                candidates = sells
                status = "selling"
            else:
                candidates = {a: d for a, d in deltas.items() if d > 1e-10}
                status = "buying"
            plan = replace(plan, status=status)
            self.records[plan_id] = plan
            for asset, delta in sorted(candidates.items()):
                price = broker._market_state.prices[asset]
                context = broker.context_for(
                    asset,
                    price,
                    "plan_check",
                    broker.estimate_order_fees(asset, delta, price, plan_id),
                )
                decision = controller.check(
                    Intent(
                        asset,
                        delta,
                        plan_id,
                        Kind.REBALANCE,
                        target_weight=plan.targets[asset],
                        rebalance_id=plan_id,
                    ),
                    broker.constraint_state(),
                    context,
                )
                if decision.code in {"settled_cash", "cash_reserve"}:
                    if broker.unsettled_cash <= 1e-10:
                        self.cancel(plan_id, "rebalance_plan_cash_budget_changed")
                        break
                    plan = replace(self.records[plan_id], status="waiting_cash")
                    self.records[plan_id] = plan
                    self.audit(plan, Action.DEFER, "rebalance_wait_settlement")
                    continue
                if decision.action is Action.DEFER or decision.code == "entry_window":
                    self.audit(plan, Action.DEFER, "rebalance_plan_deferred")
                    continue
                if decision.action is not Action.ALLOW:
                    self.cancel(plan_id, "rebalance_plan_target_illegal")
                    break
                order = broker.order_target_percent(
                    asset, plan.targets[asset], rebalance_id=plan_id
                )
                if order is not None:
                    if order.order_id not in self.records[plan_id].order_ids:
                        plan = replace(
                            self.records[plan_id],
                            order_ids=(*self.records[plan_id].order_ids, order.order_id),
                        )
                        self.records[plan_id] = plan
                    if order.status in {OrderStatus.REJECTED, OrderStatus.CANCELLED}:
                        self.cancel(plan_id, "rebalance_plan_order_terminated")
                        break
