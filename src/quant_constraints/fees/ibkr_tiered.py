"""IBKR Pro Tiered US stocks: read-only quotes and idempotent confirmed fills.

Published broker/venue snapshot: 2026-10-10. Regulatory coverage: 2026 only.
Not a statement reconciliation model: see docs/us-cash-concentrated.md.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType

from ..calendar import NY
from ..models import aware

SOURCE = "https://www.interactivebrokers.com/en/pricing/commissions-stocks.php"
SEC_SOURCE = "https://www.sec.gov/files/rules/other/2026/34-104909.pdf"
TAF_SOURCE = "https://www.finra.org/sites/default/files/2024-11/sr-finra-2024-019.pdf"
HOLIDAY_SOURCE = "https://www.govinfo.gov/content/pkg/FR-2026-09-23/html/2026-19392.htm"
TIERS = (
    (300_000, 0.0035),
    (3_000_000, 0.002),
    (20_000_000, 0.0015),
    (100_000_000, 0.001),
    (math.inf, 0.0005),
)


def finite_nonnegative(value: float, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"Invalid {name}")


@dataclass(frozen=True)
class RegulatoryRate:
    start: date
    end: date
    sec_per_dollar: float
    taf_per_share: float
    taf_cap: float
    cat_per_share: float = 0.000003
    version: str = "custom"
    sources: tuple[str, ...] = ("user-supplied regulatory schedule; verify externally",)

    def __post_init__(self):
        if self.end < self.start:
            raise ValueError("Invalid regulatory interval")
        for name in ("sec_per_dollar", "taf_per_share", "taf_cap", "cat_per_share"):
            finite_nonnegative(getattr(self, name), name)
        if not self.version or not self.sources:
            raise ValueError("Regulatory rates require version and sources")


# SEC effective charge dates, TAF 2026 rate and explicit Q4 holiday. CAT is a
# disclosed 2026-10-10 snapshot assumption, not an invented historical archive.
REGULATORY_RATES = (
    RegulatoryRate(
        date(2026, 1, 1),
        date(2026, 4, 3),
        0,
        0.000195,
        9.79,
        version="2026-pre-april;CAT-snapshot-2026-10-10",
        sources=(SEC_SOURCE, TAF_SOURCE, SOURCE),
    ),
    RegulatoryRate(
        date(2026, 4, 4),
        date(2026, 9, 30),
        0.0000206,
        0.000195,
        9.79,
        version="2026-april;CAT-snapshot-2026-10-10",
        sources=(SEC_SOURCE, TAF_SOURCE, SOURCE),
    ),
    RegulatoryRate(
        date(2026, 10, 1),
        date(2026, 12, 31),
        0.0000206,
        0,
        0,
        version="2026-Q4-TAF-holiday;CAT-snapshot-2026-10-10",
        sources=(SEC_SOURCE, TAF_SOURCE, HOLIDAY_SOURCE, SOURCE),
    ),
)


@dataclass(frozen=True)
class FeeExecutionContext:
    order_id: str
    timestamp: datetime
    side: str
    generation: int = 0  # successful amendment starts a new minimum lifecycle
    venue: str = "unknown"
    liquidity: str = "unknown"
    routing: str = "smart"
    market: str = "US"
    instrument: str = "stock"
    asset: str = "unknown"

    def __post_init__(self):
        aware(self.timestamp)
        if not self.order_id or not self.asset or self.side not in {"BUY", "SELL"}:
            raise ValueError("Fees require order ID and buy/sell direction")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("Invalid order generation")
        if self.routing != "smart":
            raise ValueError("Directed API routing is not eligible for IBKR Tiered")
        if self.market != "US" or self.instrument not in {"stock", "etf"}:
            raise ValueError("US fee model supports US stock/ETF executions only")
        if self.liquidity not in {"unknown", "add", "remove"}:
            raise ValueError("Invalid liquidity flag")

    @property
    def month(self) -> str:
        return self.timestamp.astimezone(NY).strftime("%Y-%m")

    @property
    def bucket(self) -> tuple[str, int, date]:
        return (self.order_id, self.generation, self.timestamp.astimezone(NY).date())


@dataclass(frozen=True)
class FeeBreakdown:
    broker_commission: float
    exchange_ecn_fees_or_rebates: float
    clearing_fees: float
    regulatory_fees: float
    pass_through_fees: float
    sec_fees: float
    taf_fees: float
    cat_fees: float
    monthly_volume_before: float
    eligible_volume_delta: float
    commission_cap_applied: bool
    rate_version: str
    assumptions: tuple[str, ...]
    sources: tuple[str, ...]

    @property
    def total_fees(self) -> float:
        return (
            self.broker_commission
            + self.exchange_ecn_fees_or_rebates
            + self.clearing_fees
            + self.regulatory_fees
            + self.pass_through_fees
        )


@dataclass(frozen=True)
class _OrderCosts:
    raw: float = 0
    whole_value: float = 0
    whole_quantity: float = 0
    broker_whole: float = 0
    counted_whole: float = 0


@dataclass(frozen=True)
class FeeQuote:
    context: FeeExecutionContext
    quantity: float
    price: float
    fees: FeeBreakdown
    revision: int
    order_costs: _OrderCosts


@dataclass(frozen=True)
class FeeRecord:
    execution_id: str
    context: FeeExecutionContext
    quantity: float
    price: float
    fees: FeeBreakdown


class IBKRProTieredUSStock:
    """Single simulated direct-client account; absent external volume is zero.

    Estimates never mutate state. All confirmed US/CA Tiered stock/ETF volume
    can be supplied through record_external_volume (no Canadian fee/FX model).
    Capped whole-share orders are excluded. Fractional execution components
    use the published greater-of-1%-or-$0.01 rule, without cent rounding.
    """

    def __init__(
        self,
        *,
        initial_monthly_volume=0.0,
        initial_month=None,
        unknown_venue_per_share=0.0035,
        unknown_venue_rate=0.0035,
        regulatory_rates=REGULATORY_RATES,
    ):
        for value, name in (
            (initial_monthly_volume, "initial volume"),
            (unknown_venue_per_share, "unknown venue fee"),
            (unknown_venue_rate, "unknown low-price venue fee"),
        ):
            finite_nonnegative(value, name)
        if initial_month is not None:
            parsed = date.fromisoformat(initial_month + "-01")
            if parsed.strftime("%Y-%m") != initial_month:
                raise ValueError("initial_month must be YYYY-MM")
        if initial_monthly_volume and initial_month is None:
            raise ValueError("Nonzero initial volume requires initial_month")
        self.initial_monthly_volume = float(initial_monthly_volume)
        self.initial_month = initial_month
        self.unknown_venue_per_share = unknown_venue_per_share
        self.unknown_venue_rate = unknown_venue_rate
        self.regulatory_rates = tuple(regulatory_rates)
        for left, right in zip(self.regulatory_rates, self.regulatory_rates[1:]):
            if right.start <= left.end:
                raise ValueError("Overlapping/unsorted regulatory versions")
        self._volumes: dict[str, float] = {}
        self._orders: dict[tuple[str, int, date], _OrderCosts] = {}
        self._records: dict[str, FeeRecord] = {}
        self._external: dict[str, tuple] = {}
        self._revision = 0
        self._last_time: datetime | None = None

    @property
    def monthly_volumes(self):
        return MappingProxyType(dict(self._volumes))

    @property
    def records(self) -> tuple[FeeRecord, ...]:
        return tuple(self._records.values())

    @property
    def external_executions(self):
        return MappingProxyType(dict(self._external))

    def volume(self, month: str) -> float:
        return self._volumes.get(
            month, self.initial_monthly_volume if month == self.initial_month else 0.0
        )

    @staticmethod
    def marginal(quantity: float, volume: float) -> float:
        cost = 0.0
        for threshold, rate in TIERS:
            part = min(quantity, max(0.0, threshold - volume))
            cost += part * rate
            quantity -= part
            volume += part
            if quantity <= 0:
                break
        return cost

    def quote(
        self, context: FeeExecutionContext, quantity: float, price: float, *, conservative=False
    ) -> FeeQuote:
        finite_nonnegative(quantity, "quantity")
        finite_nonnegative(price, "price")
        if quantity == 0 or price == 0:
            raise ValueError("Fees require positive filled quantity and price")
        if self._last_time is not None and context.timestamp < self._last_time:
            raise ValueError("Fee executions must be chronological")
        day = context.timestamp.astimezone(NY).date()
        regime = next((r for r in self.regulatory_rates if r.start <= day <= r.end), None)
        if regime is None:
            raise ValueError(f"No explicit regulatory fee version for {day}")
        volume = self.volume(context.month)
        old = _OrderCosts() if conservative else self._orders.get(context.bucket, _OrderCosts())
        whole = float(math.floor(quantity))
        fractional = quantity - whole
        raw = old.raw + self.marginal(whole, 0 if conservative else volume)
        whole_value = old.whole_value + whole * price
        gross = max(raw, 0.35) if old.whole_quantity + whole > 0 else 0.0
        capped = gross > 0.01 * whole_value and whole_value > 0
        cumulative = min(gross, 0.01 * whole_value)
        broker_fee = cumulative - old.broker_whole
        if fractional > 1e-12:
            broker_fee += max(0.01, 0.01 * fractional * price)
        counted = 0.0 if capped else old.whole_quantity + whole
        delta = counted - old.counted_whole + fractional
        costs = _OrderCosts(raw, whole_value, old.whole_quantity + whole, cumulative, counted)
        assumptions = [
            "single-account; absent external US/CA Tiered volume assumed zero",
            "whole/fractional components split per execution; no cent rounding",
            "online cap eligibility; capped whole components excluded",
            "CAT uses 2026-10-10 snapshot, not historical CAT archive",
            "published broker/venue snapshot 2026-10-10; taxes excluded",
        ]
        # Only displayed regular-hours NASDAQ/ARCA/IEX schedules are modeled.
        # Unknown routing is an explicit scenario, not a universal upper bound.
        if context.venue in {"NASDAQ", "ARCA", "IEX"} and context.liquidity != "unknown":
            if context.liquidity == "add":
                exchange = 0.0
                assumptions.append("no rebate credit, even for known adding-liquidity fills")
            elif context.venue == "IEX" and price < 1:
                exchange = quantity * price * 0.002
            else:
                exchange = quantity * (0.003 if price >= 1 else price * 0.003)
            venue_source = {"NASDAQ": "INET", "ARCA": "ARCA", "IEX": "IEX"}[context.venue]
            venue_source = (
                f"https://www.interactivebrokers.com/en/accounts/fees/{venue_source}stkfee.php"
            )
            assumptions.append("displayed regular-hours execution schedule assumed")
        else:
            exchange = quantity * (
                self.unknown_venue_per_share if price >= 1 else price * self.unknown_venue_rate
            )
            venue_source = "https://www.interactivebrokers.com/en/accounts/fees/ARCAstkfee.php"
            assumptions.append(
                "unknown venue/liquidity: conservative ARCA-routed scenario; no rebate"
            )
        clearing = min(quantity * 0.0002, quantity * price * 0.005)
        sec = quantity * price * regime.sec_per_dollar if context.side == "SELL" else 0.0
        taf = (
            min(quantity * regime.taf_per_share, regime.taf_cap) if context.side == "SELL" else 0.0
        )
        cat = quantity * regime.cat_per_share
        passthrough = broker_fee * (0.000175 + 0.00056)
        fees = FeeBreakdown(
            broker_fee,
            exchange,
            clearing,
            sec + taf + cat,
            passthrough,
            sec,
            taf,
            cat,
            volume,
            delta,
            capped,
            regime.version,
            tuple(assumptions),
            (SOURCE, venue_source, *regime.sources),
        )
        if fees.total_fees < 0 or not math.isfinite(fees.total_fees):
            raise ValueError("Invalid total fees")
        return FeeQuote(context, quantity, price, fees, self._revision, costs)

    def estimate(self, context: FeeExecutionContext, quantity: float, price: float) -> FeeBreakdown:
        """Conservative fresh-minimum, first-tier reservation; no state mutation."""
        return self.quote(context, quantity, price, conservative=True).fees

    def commit(self, execution_id: str, quote: FeeQuote) -> FeeRecord:
        """Confirm exactly once after canonical fill acceptance, never on a quote."""
        if not execution_id or execution_id in self._external:
            raise ValueError("Invalid/duplicate execution ID")
        record = FeeRecord(execution_id, quote.context, quote.quantity, quote.price, quote.fees)
        if execution_id in self._records:
            previous = self._records[execution_id]
            if previous != record:
                raise ValueError("Conflicting duplicate execution")
            return previous
        if quote.revision != self._revision:
            raise ValueError("Stale fee quote; requote before accepting fill")
        if quote != self.quote(quote.context, quote.quantity, quote.price):
            raise ValueError("Fee quote must match current execution state")
        self._volumes[quote.context.month] = (
            self.volume(quote.context.month) + quote.fees.eligible_volume_delta
        )
        self._orders[quote.context.bucket] = quote.order_costs
        self._records[execution_id] = record
        self._last_time = quote.context.timestamp
        self._revision += 1
        return record

    def record_external_volume(
        self,
        execution_id: str,
        timestamp: datetime,
        quantity: float,
        *,
        market: str,
        instrument: str,
        tiered=True,
        capped=False,
    ):
        """Confirmed external US/CA shares only; no simulated cash/fee mutation."""
        aware(timestamp)
        finite_nonnegative(quantity, "external quantity")
        if not execution_id or execution_id in self._records or quantity <= 0:
            raise ValueError("Invalid external execution")
        if market not in {"US", "CA"} or instrument not in {"stock", "etf"}:
            raise ValueError("External volume must be US/CA stock/ETF shares")
        if type(tiered) is not bool or type(capped) is not bool:
            raise ValueError("Invalid external pricing flags")
        identity = (timestamp, quantity, market, instrument, tiered, capped)
        if execution_id in self._external:
            if self._external[execution_id] != identity:
                raise ValueError("Conflicting external execution")
            return
        if self._last_time is not None and timestamp < self._last_time:
            raise ValueError("External executions must be chronological")
        month = timestamp.astimezone(NY).strftime("%Y-%m")
        self._volumes[month] = self.volume(month) + (quantity if tiered and not capped else 0)
        self._external[execution_id] = identity
        self._last_time = timestamp
        self._revision += 1
