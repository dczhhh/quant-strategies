"""Atomic pre-execution transformations of the broker's canonical state.

No synthetic trades, no shadow positions, no payable-date cash assumption. Applied
event identities and entitlements live in AccountState and survive its checkpoints.
"""

import copy
import math
from dataclasses import asdict, replace
from datetime import date, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING

from ml4t.backtest.risk.position.composite import AllOf, AnyOf, RuleChain
from ml4t.backtest.risk.position.dynamic import (
    ScaledExit,
    TighteningTrailingStop,
    TrailingStop,
    VolatilityStop,
    VolatilityTrailingStop,
)
from ml4t.backtest.risk.position.signal import SignalExit
from ml4t.backtest.risk.position.static import StopLoss, TakeProfit, TimeExit
from ml4t.backtest.types import OrderSide, OrderStatus

from ..calendar import NY
from ..models import aware
from .events import CorporateAction, CorporateActionProvider

if TYPE_CHECKING:
    from ..adapter import ConstrainedBroker


def entitlement_key(security_id: str, event_id: str) -> str:
    return f"{len(security_id)}:{security_id}:{event_id}"


class CorporateActionProcessor:
    def __init__(self, broker: "ConstrainedBroker", provider: CorporateActionProvider):
        self.broker, self.provider = broker, provider
        self.failures: list[dict] = []  # diagnostics only, never an accounting owner
        if not hasattr(broker.account, "_corporate_action_state"):
            broker.account._corporate_action_state = {  # type: ignore[unresolved-attribute]
                "processed": {},
                "identities": {},
                "entitlements": {},
                "records": [],
                "scopes": {},
                "revisions": [],
                "last_asof": None,
            }
        # Legacy checkpoints have no proof of zero historical exposure. Treat
        # those processed events as economically applied, never downgrade them.
        self.state.setdefault("scopes", {})
        self.state.setdefault("revisions", [])

    @property
    def state(self):
        return self.broker.account._corporate_action_state  # type: ignore[unresolved-attribute]

    def has_exposure(self, asset: str | None) -> bool:
        broker = self.broker
        return asset is not None and (
            asset in broker.positions
            or asset in broker._risk_state.pending_exits
            or any(o.asset == asset and o.status is OrderStatus.PENDING for o in broker.orders)
            or any(
                plan.active and asset in plan.reference_prices
                for plan in broker.plan_manager.records.values()
            )
        )

    def prepare(self, asof: datetime, observed: set[str]):
        """Read-only preflight. Source gaps/late economics reject before bar mutation."""
        aware(asof)
        last = self.state["last_asof"]
        if last is not None and asof < last:
            raise ValueError("Corporate action observations must be chronological")
        day = asof.astimezone(NY).date()
        broker = self.broker
        manager = broker._preopen_target_manager
        if manager is not None and manager.target_count:
            raise ValueError(
                "Canonical pre-open intents require explicit corporate action reconciliation"
            )
        assets = (
            observed
            | set(broker.positions)
            | {o.asset for o in broker.orders if o.status is OrderStatus.PENDING}
            | set(broker._risk_state.pending_exits)
            | {
                asset
                for plan in broker.plan_manager.records.values()
                if plan.active
                for asset in plan.reference_prices
            }
        )
        identities = {}
        for asset in sorted(assets):
            context = broker.context_provider(asof, asset, "corporate_action")
            if context.asof != asof:
                raise ValueError("Corporate action context must match current asof")
            if not isinstance(context.security_id, str) or not context.security_id.strip():
                raise ValueError(f"Stable security_id required for {asset}")
            if context.execution_data_mode != "raw_execution":
                raise ValueError(f"Corporate actions require raw_execution OHLCV for {asset}")
            if context.risk_data_mode != "raw_execution":
                raise ValueError(
                    f"Corporate actions require raw_execution price-valued risk inputs for {asset}"
                )
            if context.signal_data_mode != broker.controller.config.signal_data_mode:
                raise ValueError(f"Signal adjustment mode mismatch for {asset}")
            old = self.state["identities"].get(asset)
            exposed = self.has_exposure(asset)
            if old is not None and old != context.security_id and exposed:
                raise ValueError(f"Ticker identity change with exposure: {asset}: {old}")
            identities[asset] = context.security_id
        if len(set(identities.values())) != len(identities):
            raise ValueError("Duplicate tickers for one security require explicit migration")
        securities = set(identities.values()) | {
            key[0] for key, item in self.state["entitlements"].items() if not item["credited"]
        }
        by_security = {sid: asset for asset, sid in identities.items()}
        events = []
        for sid in sorted(securities):
            coverage, supplied = self.provider.snapshot(sid, asof)
            start = last.astimezone(NY).date() if last is not None else day
            if (
                coverage is None
                or coverage.security_id != sid
                or coverage.available_at > asof
                or coverage.missing
                or not coverage.point_in_time
                or coverage.covered_from > start
                or coverage.covered_until < day
            ):
                raise ValueError(f"Missing point-in-time corporate action coverage: {sid}")
            latest: dict[str, CorporateAction] = {}
            for event in sorted(supplied, key=lambda e: (e.available_at, e.version)):
                if event.security_id == sid and event.available_at <= asof:
                    previous = latest.get(event.event_id)
                    if (
                        previous
                        and (previous.available_at, previous.version)
                        == (event.available_at, event.version)
                        and previous.economics != event.economics
                    ):
                        raise ValueError(f"Conflicting corporate action revision: {event.key}")
                    latest[event.event_id] = event
            for event in latest.values():
                applied = self.state["processed"].get(event.key)
                if applied is not None:
                    scope = self.state.get("scopes", {}).get(event.key)
                    if applied.economics != event.economics and scope != "observed_only":
                        raise ValueError(
                            f"Applied corporate action revised; reconcile checkpoint: {event.key}"
                        )
                    if scope == "observed_only" and applied != event:
                        # The zero-exposure proof belongs to this historical
                        # event, not to today's holdings. A changed date/kind
                        # needs a new proof and cannot reuse that observation.
                        if (
                            applied.kind != event.kind
                            or applied.effective_date != event.effective_date
                        ):
                            raise ValueError(
                                "Observed corporate action scope revised; reconcile checkpoint: "
                                f"{event.key}"
                            )
                        effective = broker.controller.calendar.on_or_after(event.effective_date)
                        events.append((effective, by_security.get(sid), event, True))
                    continue
                if event.cancelled:
                    continue
                effective = (
                    event.effective_date
                    if event.kind == "CASH_CREDIT"
                    else broker.controller.calendar.on_or_after(event.effective_date)
                )
                if effective > day:
                    continue
                asset = by_security.get(sid)
                exposed = self.has_exposure(asset)
                session = broker.controller.calendar.session(effective)
                traded_after_effective = session is not None and any(
                    fill.asset == asset and fill.timestamp >= session.market_open
                    for fill in broker.fills
                )
                if (
                    event.kind != "CASH_CREDIT"
                    and (exposed or traded_after_effective)
                    and session is not None
                    and last is not None
                    and last >= session.market_open
                ):
                    raise ValueError(
                        f"Late corporate action after effective-session observation: {event.key}"
                    )
                pos = broker.positions.get(asset) if asset is not None else None
                if (
                    event.kind in {"SPLIT", "CASH_DIVIDEND"}
                    and pos is not None
                    and pos.entry_time.astimezone(NY).date() >= effective
                ):
                    raise ValueError(f"Cannot reconstruct pre-event entitlement/basis: {event.key}")
                if exposed or event.kind == "CASH_CREDIT":
                    self.validate_terms(event)
                events.append((effective, asset, event, False))
        rank = {"SPLIT": 0, "CASH_DIVIDEND": 1, "CASH_CREDIT": 2}
        events.sort(key=lambda item: (item[0], rank.get(item[2].kind, 3), item[2].key))
        return identities, events

    @staticmethod
    def validate_terms(event: CorporateAction):
        if event.kind not in {"SPLIT", "CASH_DIVIDEND", "CASH_CREDIT"}:
            raise ValueError(f"Unsupported exposed corporate action: {event.key}: {event.kind}")
        if event.currency != "USD" or event.terms != "ordinary":
            raise ValueError(f"Unsupported currency/special corporate action terms: {event.key}")
        if event.kind == "SPLIT" and event.fractional_policy != "retain":
            raise ValueError(
                f"Cash-in-lieu requires authoritative entitlement and credit: {event.key}"
            )
        if event.kind == "CASH_DIVIDEND" and event.dividend_basis != "post_split":
            raise ValueError(f"Dividend must declare post-split per-share basis: {event.key}")

    def apply(self, asof: datetime, observed: set[str], prepared):
        identities, events = prepared
        # A legacy snapshot may be restored into an existing processor. Backfill
        # only on the transactional write path, keeping preflight read-only.
        self.state.setdefault("scopes", {})
        self.state.setdefault("revisions", [])
        self.state["identities"].update(identities)
        for effective, asset, event, observation_revision in events:
            if observation_revision:
                previous = self.state["processed"][event.key]
                self.state["revisions"].append(
                    {
                        "security_id": event.security_id,
                        "event_id": event.event_id,
                        "previous_version": previous.version,
                        "previous_source": previous.source,
                        "previous_terms": self.event_snapshot(previous),
                        "version": event.version,
                        "source": event.source,
                        "available_at": event.available_at.isoformat(),
                        "observed_at": asof.isoformat(),
                        "scope": "observed_only",
                        "terms": self.event_snapshot(event),
                    }
                )
                self.state["processed"][event.key] = event
                continue
            quantity = (
                self.broker.account.get_position_quantity(asset) if asset is not None else 0.0
            )
            record = {
                "security_id": event.security_id,
                "event_id": event.event_id,
                "kind": event.kind,
                "source": event.source,
                "version": event.version,
                "available_at": event.available_at.isoformat(),
                "processed_at": asof.isoformat(),
                "effective_date": effective.isoformat(),
                "currency": event.currency,
                "asset": asset,
                "eligible_quantity": quantity,
                "gross": 0.0,
                "withholding": 0.0,
                "fees": 0.0,
                "income_delta": 0.0,
                "cash_delta": 0.0,
                "recovered_after_gap": effective < asof.astimezone(NY).date(),
                "missing_asset_bar": asset not in observed,
                "status": "ignored_unexposed",
            }
            scope = "observed_only"
            if event.kind == "SPLIT" and asset is not None and self.has_exposure(asset):
                self.split(asset, event, observed)
                record.update(status="split_applied", ratio=event.split_ratio)
                scope = "economically_applied"
            elif event.kind == "CASH_DIVIDEND":
                self.dividend(event, quantity, record)
                if quantity:
                    scope = "economically_applied"
            elif event.kind == "CASH_CREDIT":
                self.credit(event, record)
                scope = "economically_applied"
            record["scope"] = scope
            self.state["processed"][event.key] = event
            self.state["scopes"][event.key] = scope
            self.state["records"].append(record)
        self.state["last_asof"] = asof

    @staticmethod
    def event_snapshot(event: CorporateAction) -> dict:
        return {
            name: value.isoformat() if isinstance(value, (date, datetime)) else value
            for name, value in asdict(event).items()
        }

    def split(self, asset: str, event: CorporateAction, observed: set[str]):
        broker = self.broker
        ratio = event.split_ratio
        assert ratio is not None
        pos = broker.positions.get(asset)
        if pos is not None:
            adjusted = pos.quantity * ratio
            if not math.isfinite(adjusted) or not math.isclose(
                adjusted,
                round(adjusted, event.quantity_precision),
                rel_tol=0,
                abs_tol=8 * math.ulp(adjusted),
            ):
                raise ValueError(
                    f"Split entitlement exceeds source tradable precision: {event.key}"
                )
            pos.quantity = adjusted  # retain entitlement, never silently round away basis
            if pos.initial_quantity is not None:
                pos.initial_quantity *= ratio
            for name in (
                "entry_price",
                "current_price",
                "high_water_mark",
                "low_water_mark",
                "entry_slippage",
            ):
                value = getattr(pos, name)
                if value is not None:
                    setattr(pos, name, value / ratio)
            if not all(
                math.isfinite(getattr(pos, name)) and getattr(pos, name) > 0
                for name in ("entry_price", "current_price", "high_water_mark", "low_water_mark")
            ):
                raise ValueError(f"Split creates non-finite/zero position prices: {event.key}")
            if "signal_price" in pos.context:
                pos.context["signal_price"] /= ratio
            quote = pos.context.get("entry_quote_context", {})
            for name in ("reference_price", "quote_mid_price", "bid_price", "ask_price", "spread"):
                if quote.get(name) is not None:
                    quote[name] /= ratio
            for name in ("bid_size", "ask_size", "available_size"):
                if quote.get(name) is not None:
                    quote[name] *= ratio
            rules = broker._risk_state.position_rules_by_asset.get(asset)
            if rules is None:
                # Isolate stateful/absolute rules without changing other assets' rules.
                rules = copy.deepcopy(broker._risk_state.position_rules)
                if rules is not None:
                    broker._risk_state.position_rules_by_asset[asset] = rules
            self.scale_rule(rules, ratio, pos.context)
        pending = broker._risk_state.pending_exits.get(asset)
        if pending:
            pending["quantity"] *= ratio
            if pending.get("fill_price") is not None:
                pending["fill_price"] /= ratio
        if asset not in broker._market_state.prices and asset in broker._market_state.last_prices:
            broker._market_state.last_prices[asset] /= ratio
        live_groups = {
            parent
            for parent, children in broker.bracket_children.items()
            if any(
                (order := broker.get_order(i)) is not None and order.status is OrderStatus.PENDING
                for i in (parent, *children)
            )
        }
        for order in broker.orders:
            if order.asset != asset or not (
                order.status is OrderStatus.PENDING
                or order.order_id in live_groups
                or order.parent_id in live_groups
            ):
                continue
            # Terminal bracket ancestors still carry current-unit exposure for OCO.
            for name in ("quantity", "filled_quantity", "requested_quantity"):
                value = getattr(order, name)
                if value is not None:
                    setattr(order, name, value * ratio)
            for name in (
                "limit_price",
                "stop_price",
                "trail_amount",
                "filled_price",
                "_signal_price",
                "_risk_fill_price",
                "_reservation_price",
            ):
                value = getattr(order, name)
                if value is not None:
                    setattr(order, name, value / ratio)
            for name in (
                "limit_price",
                "stop_price",
                "filled_price",
                "_signal_price",
                "_risk_fill_price",
                "_reservation_price",
                "trail_amount",
            ):
                value = getattr(order, name)
                if value is not None and (
                    not math.isfinite(value) or value < 0 or (value == 0 and name != "trail_amount")
                ):
                    raise ValueError(f"Invalid split-adjusted order price: {order.order_id}")
            identifier = order.order_id
            if not math.isfinite(order.quantity) or not math.isclose(
                order.quantity,
                round(order.quantity, event.quantity_precision),
                rel_tol=0,
                abs_tol=8 * math.ulp(order.quantity),
            ):
                raise ValueError(f"Split order quantity exceeds tradable precision: {identifier}")
            for mapping in (
                broker.order_requested,
                broker.order_permitted,
                broker._order_state.partial_quantities,
            ):
                if identifier in mapping:
                    mapping[identifier] *= ratio
            if identifier in broker.order_audit_state:
                qty, status = broker.order_audit_state[identifier]
                broker.order_audit_state[identifier] = qty * ratio, status
            protective = (
                order.parent_id in live_groups or broker.constraint_kinds.get(identifier) == "risk"
            )
            if (
                broker.controller.config.split_order_policy == "cancel"
                and order.status is OrderStatus.PENDING
                and not protective
            ):
                if order.rebalance_id in broker.plan_manager.records:
                    broker.plan_manager.cancel(order.rebalance_id, "split_canceled_rebalance_plan")
                else:
                    broker.cancel_constraint_order(
                        identifier, "split_canceled_remainder_protection_retained"
                    )
            elif order.status is OrderStatus.PENDING and order.side is OrderSide.BUY:
                remaining = broker._order_state.partial_quantities.get(identifier, order.quantity)
                price = order._reservation_price
                if price is None or price <= 0:
                    raise ValueError(f"Missing split reservation price: {identifier}")
                fee = broker.estimate_order_fees(asset, remaining, price, identifier)
                order._reserved_cash = remaining * price + fee
        for identifier, plan in tuple(broker.plan_manager.records.items()):
            if plan.active and asset in plan.reference_prices:
                prices = dict(plan.reference_prices)
                prices[asset] /= ratio
                broker.plan_manager.records[identifier] = replace(
                    plan, reference_prices=MappingProxyType(prices)
                )
        broker.refresh_brackets()
        assert broker._cash_account_rules is not None
        if broker._cash_account_rules.reserved_cash() > broker.settled_cash + 1e-8:
            raise ValueError(
                "Split-adjusted order fees exceed settled cash; explicit cancellation required"
            )

    @classmethod
    def scale_rule(cls, rule, ratio: float, context: dict, scaled_keys: set | None = None):
        if scaled_keys is None:
            scaled_keys = set()
        if rule is None:
            return
        if isinstance(rule, (list, tuple)):
            for child in rule:
                cls.scale_rule(child, ratio, context, scaled_keys)
        elif type(rule) in (RuleChain, AllOf, AnyOf):
            for child in rule.rules:
                cls.scale_rule(child, ratio, context, scaled_keys)
        elif type(rule) in (VolatilityStop, VolatilityTrailingStop):
            # Shared ATR keys are converted once, not once per rule.
            if rule.atr_key in context and rule.atr_key not in scaled_keys:
                context[rule.atr_key] /= ratio
                scaled_keys.add(rule.atr_key)
            if isinstance(rule, VolatilityStop) and rule._entry_atr is not None:
                rule._entry_atr /= ratio
        elif type(rule) not in (
            StopLoss,
            TakeProfit,
            TimeExit,
            TrailingStop,
            TighteningTrailingStop,
            ScaledExit,
            SignalExit,
        ):
            raise ValueError(
                f"Unspecified split semantics for custom position rule: {type(rule).__name__}"
            )

    def dividend(self, event: CorporateAction, quantity: float, record: dict):
        assert event.dividend_per_share is not None
        rate = (
            event.withholding_rate
            if event.withholding_rate is not None
            else self.broker.controller.config.dividend_withholding_rate
        )
        gross = quantity * event.dividend_per_share
        withholding, fees = gross * rate, quantity * event.fee_per_share
        net = gross - withholding - fees
        if net < 0:
            raise ValueError(f"Dividend deductions exceed gross: {event.key}")
        self.broker.account._receivables[entitlement_key(*event.key)] = net
        self.state["entitlements"][event.key] = {
            "net": net,
            "gross": gross,
            "quantity": quantity,
            "credited": False,
        }
        record.update(
            status="dividend_receivable",
            gross=gross,
            withholding=withholding,
            fees=fees,
            income_delta=net,
            withholding_rate=rate,
            tax_scenario=(
                "source_account_withholding"
                if event.withholding_rate is not None
                else self.broker.controller.config.dividend_tax_scenario
            ),
            record_date=event.record_date.isoformat() if event.record_date else None,
            payable_date=event.payable_date.isoformat() if event.payable_date else None,
        )

    def credit(self, event: CorporateAction, record: dict):
        assert event.parent_event_id is not None
        parent = event.security_id, event.parent_event_id
        item = self.state["entitlements"].get(parent)
        if item is None or item["credited"]:
            raise ValueError(f"Unknown/already credited dividend entitlement: {parent}")
        estimate = item["net"]
        actual = estimate if event.credited_net is None else event.credited_net
        if actual > item["gross"] + 1e-8:
            raise ValueError(f"Confirmed credit exceeds eligible gross entitlement: {parent}")
        self.broker.account.cash += actual
        self.broker.account._lock_notional_free_cash += actual
        self.broker.account._receivables.pop(entitlement_key(*parent))
        item["credited"] = True
        record.update(
            status="cash_credited",
            eligible_quantity=item.get(
                "quantity",
                next(
                    (
                        r["eligible_quantity"]
                        for r in self.state["records"]
                        if (r["security_id"], r["event_id"]) == parent
                    ),
                    0.0,
                ),
            ),
            cash_delta=actual,
            income_delta=actual - estimate,
            parent_event_id=event.parent_event_id,
        )

    def evidence(self):
        records = copy.deepcopy(self.state["records"])
        return {
            "version": 1,
            "data_modes": {
                "execution": "raw_execution",
                "risk": "raw_execution",
                "signal": self.broker.controller.config.signal_data_mode,
            },
            "split_order_policy": self.broker.controller.config.split_order_policy,
            "records": records,
            "observation_revisions": copy.deepcopy(self.state.get("revisions", [])),
            "income": sum(r["income_delta"] for r in records),
            "receivables": dict(self.broker.account._receivables),
            "outstanding": self.broker.account._receivable_value,
            "failures": copy.deepcopy(self.failures),
        }
