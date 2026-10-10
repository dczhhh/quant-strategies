"""Point-in-time execution costs; no order, cash or position ownership.

Regime bps are scenario assumptions for liquid large/mid-cap equities, not an
empirical impact model. The upstream four-argument SlippageModel is preserved
by binding an execution snapshot in the opt-in adapter.
"""

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time

from .calendar import NY, SessionCalendar
from .config import ConstraintConfig
from .events import EarningsEvent, EarningsProvider
from .macro import MacroEventGate
from .models import MarketContext


@dataclass(frozen=True)
class SlippageQuote:
    asset: str
    timestamp: datetime
    quantity: float
    reference_price: float
    slippage_regime: str
    slippage_bps: float
    basis: tuple[str, ...]
    macro_incremental_bps: float = 0.0

    @property
    def per_share(self):
        return self.reference_price * self.slippage_bps / 10_000

    @property
    def slippage_amount(self):
        return self.quantity * self.per_share


@dataclass(frozen=True)
class SlippageRecord:
    execution_id: str
    order_id: str
    side: str
    quote: SlippageQuote


class RegimeSlippage:
    def __init__(
        self,
        config: ConstraintConfig,
        calendar: SessionCalendar,
        earnings: EarningsProvider,
        context_provider: Callable[[str], MarketContext] | None = None,
        macro: MacroEventGate | None = None,
    ):
        self.config, self.calendar, self.earnings = config, calendar, earnings
        self.context_provider = context_provider
        self.macro = macro or MacroEventGate(config, calendar)
        self.bound: tuple[MarketContext, bool] | None = None
        self.actual_quote: SlippageQuote | None = None
        self._records: dict[str, SlippageRecord] = {}

    @property
    def records(self):
        return tuple(self._records.values())

    def affected_session(self, event: EarningsEvent):
        """Actual release time, including half-day closes, determines the session."""
        return self.calendar.first_affected_session(event.announcement_at)

    def quote(self, asset, quantity, price, context: MarketContext, *, reservation=False):
        settings = self.config
        day = context.asof.astimezone(NY).date()
        session = self.calendar.session(day)
        basis = ["NYSE session calendar; execution snapshot=" + context.asof.isoformat()]
        regular = settings.slippage_regular_bps
        candidates = [(regular, "regular")]
        if session is None:
            # This is an estimate only: actual execution is independently gated
            # to observed RTH. Do not silently label absent session data regular.
            candidates.append((settings.slippage_early_close_bps, "calendar_fallback"))
            basis.append("no current NYSE session; conservative calendar fallback; no RTH fill")
        elif session.market_close.astimezone(NY).time() < time(16):
            candidates.append((settings.slippage_early_close_bps, "early_close"))
            basis.append("early close=" + session.market_close.isoformat())
        if context.instrument == "plain_sector_etf":
            basis.append("explicit plain_sector_etf: issuer earnings do not apply")
        else:
            coverage, events = self.earnings.snapshot(asset, context.asof)
            # Enforce the information boundary even for a provider returning
            # unpublished revisions. Scheduled future announcements never stress
            # an earlier fill just because the schedule is already known.
            visible = tuple(
                event
                for event in events
                if event.asset == asset
                and event.available_at <= context.asof
                and not event.cancelled
            )
            missing = (
                context.instrument != "equity"
                or coverage is None
                or coverage.asset != asset
                or coverage.available_at > context.asof
                or coverage.covered_until < context.asof
                or coverage.missing
                or any(e.missing and e.announcement_at <= context.asof for e in visible)
            )
            if missing:
                mode = settings.slippage_missing_earnings
                basis.append("missing issuer/earnings coverage; configured fallback=" + mode)
                if mode == "error":
                    raise ValueError("Missing point-in-time slippage earnings coverage")
                if mode == "stress":
                    candidates.append((settings.slippage_earnings_bps, "earnings_fallback"))
            else:
                assert coverage is not None
                basis.append("earnings coverage=" + coverage.source)
            for event in visible:
                if (
                    not event.missing
                    and event.announcement_at <= context.asof
                    and self.affected_session(event) == day
                ):
                    candidates.append((settings.slippage_earnings_bps, "earnings_affected"))
                    basis.append(
                        f"event={event.event_id}; timing={event.timing}; source={event.source}; "
                        f"announcement_at={event.announcement_at.isoformat()}; "
                        f"available_at={event.available_at.isoformat()}"
                    )
            if reservation:
                # Potential event stress is a configured upper bound, not a
                # prediction based on a future earnings revision.
                candidates.append((settings.slippage_earnings_bps, "reservation_upper_bound"))
        if reservation:
            candidates.append((settings.slippage_early_close_bps, "reservation_upper_bound"))
            basis.append("reservation uses max applicable configured bps; actual fill rechecked")
        baseline = max(value for value, _ in candidates)
        if settings.macro_events_enabled:
            missing, windows = self.macro.active(context.asof, pressure=True)
            if windows or reservation or missing:
                # Missing data never cancels a risk sell; use an auditable stress estimate.
                candidates.append((settings.macro_slippage_bps, "macro_event"))
                basis.append(
                    "macro scenario bps="
                    + str(settings.macro_slippage_bps)
                    + "; pressure uses max, not sum; missing_calendar="
                    + str(missing)
                )
                basis.extend("macro event=" + str(window.record()) for window in windows)
        bps, regime = max(candidates, key=lambda candidate: candidate[0])
        return SlippageQuote(
            asset,
            context.asof,
            abs(quantity),
            price,
            regime,
            bps,
            tuple(basis),
            max(0.0, bps - baseline),
        )

    @contextmanager
    def bind(self, context: MarketContext, *, actual=False):
        previous = self.bound, self.actual_quote
        self.bound, self.actual_quote = (context, actual), None
        try:
            yield
        finally:
            self.bound, self.actual_quote = previous

    def calculate(self, asset, quantity, price, volume):
        # Available bar volume is enforced by execution_limits before this
        # model runs. A stress bps choice never increases participation capacity.
        if self.bound is None:
            if self.context_provider is None:
                raise ValueError("RegimeSlippage needs a bound execution context")
            context, actual = self.context_provider(asset), False
        else:
            context, actual = self.bound
        quote = self.quote(asset, quantity, price, context, reservation=not actual)
        if actual:
            self.actual_quote = quote
        return quote.per_share

    def commit_fill(self, order, fill):
        quote = self.actual_quote
        if (
            quote is None
            or fill.order_id != order.order_id
            or fill.asset != quote.asset
            or fill.timestamp != quote.timestamp
            or fill.quantity != quote.quantity
            or abs(fill.slippage - quote.per_share) > 1e-10
        ):
            raise RuntimeError("Canonical fill does not match prepared slippage quote")
        identifier = f"{order.order_id}/{fill.timestamp.isoformat()}/{order.filled_quantity!r}"
        record = SlippageRecord(identifier, order.order_id, order.side.value, quote)
        existing = self._records.get(identifier)
        if existing is not None and existing != record:
            raise RuntimeError("Conflicting confirmed slippage execution")
        self._records[identifier] = record
        return record
