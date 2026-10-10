"""Read-only CommissionModel facade with explicit execution context.

The upstream protocol, generic TieredCommission and unconfigured Broker are
unchanged. All calculate/deepcopy paths only quote. The constrained settlement
hook commits after the canonical fill has been recorded and charged.
"""

from contextlib import contextmanager
from typing import TYPE_CHECKING

from .ibkr_tiered import FeeExecutionContext, FeeQuote, IBKRProTieredUSStock

if TYPE_CHECKING:
    from ml4t.backtest.types import Order

    from ..adapter import ConstrainedBroker


class FeeCommissionBridge:
    def __init__(self, broker: "ConstrainedBroker", model: IBKRProTieredUSStock):
        self.broker, self.model = broker, model
        self.generations: dict[str, int] = {}
        self.bound: tuple[FeeExecutionContext, bool, str] | None = None
        self.actual_quote: FeeQuote | None = None
        self.execution_id: str | None = None

    def __deepcopy__(self, memo):
        # This facade is read-only; do not copy the broker/account or the fee
        # ledger when upstream prechecks deepcopy a commission model.
        copy = type(self)(self.broker, self.model)
        copy.generations = dict(self.generations)
        copy.bound = self.bound
        return copy

    def context(self, asset: str, side: str, order_id: str, generation=None):
        timestamp = self.broker._market_state.time
        assert timestamp is not None
        supplied = self.broker.context_provider(timestamp, asset, "fees")
        if supplied.asof != timestamp:
            raise ValueError("Fee context must be current point-in-time")
        known = (
            supplied.fee_metadata_available_at is not None
            and supplied.fee_metadata_available_at <= timestamp
        )
        return FeeExecutionContext(
            order_id,
            timestamp,
            side,
            self.generations.get(order_id, 0) if generation is None else generation,
            supplied.execution_venue if known else "unknown",
            supplied.execution_liquidity if known else "unknown",
            instrument="etf" if supplied.instrument == "plain_sector_etf" else "stock",
            asset=asset,
        )

    def estimate(self, asset, signed_quantity, price, order_id="estimate", generation=None):
        if price <= 0:
            return 0.0  # missing-price gate handles rejection/defer; not a zero-fee fill
        context = self.context(
            asset, "BUY" if signed_quantity > 0 else "SELL", order_id, generation
        )
        return self.model.estimate(context, abs(signed_quantity), price).total_fees

    @contextmanager
    def bind(self, order: "Order", *, actual=False, generation=None):
        previous = (self.bound, self.actual_quote, self.execution_id)
        self.bound = (
            self.context(order.asset, order.side.value.upper(), order.order_id, generation),
            actual,
            order.asset,
        )
        self.actual_quote = None
        self.execution_id = (
            f"{order.order_id}/{self.bound[0].generation}/"
            f"{order.filled_quantity!r}/{self.bound[0].timestamp.isoformat()}"
        )
        try:
            yield
        finally:
            self.bound, self.actual_quote, self.execution_id = previous

    def calculate(self, asset: str, quantity: float, price: float) -> float:
        if self.bound is None:
            return self.estimate(asset, quantity, price)
        context, actual, bound_asset = self.bound
        if asset != bound_asset:
            raise ValueError("Fee quote asset differs from bound order")
        if actual:
            self.actual_quote = self.model.quote(context, abs(quantity), price)
            return self.actual_quote.fees.total_fees
        return self.model.estimate(context, abs(quantity), price).total_fees

    def commit_fill(self, order: "Order", fill):
        quote = self.actual_quote
        if (
            quote is None
            or self.execution_id is None
            or fill.order_id != order.order_id
            or fill.quantity != quote.quantity
            or fill.price != quote.price
            or abs(fill.commission - quote.fees.total_fees) > 1e-10
        ):
            raise RuntimeError("Canonical fill does not match prepared fee quote")
        return self.model.commit(self.execution_id, quote)
