"""Raw and signal products never substitute for one another implicitly."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .errors import (
    SCHEMA_VERSION,
    ErrorCode,
    currency,
    enum_value,
    fail,
    number,
    schema,
    text,
    timestamp,
)
from .provenance import SourceProvenance, source


class PriceBasis(StrEnum):
    RAW = "raw"
    SPLIT_ADJUSTED = "split_adjusted"
    TOTAL_RETURN = "total_return"


class ShareUnit(StrEnum):
    AS_TRADED = "as_traded"
    SPLIT_ADJUSTED = "split_adjusted"


class VolumeUnit(StrEnum):
    AS_TRADED_SHARES = "as_traded_shares"
    SPLIT_ADJUSTED_SHARES = "split_adjusted_shares"


@dataclass(frozen=True, slots=True, kw_only=True)
class _Bar:
    security_id: str
    symbol: str
    venue: str
    currency: str
    product_id: str
    bar_start_at: datetime
    bar_end_at: datetime
    available_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    price_basis: PriceBasis
    share_unit: ShareUnit
    volume_unit: VolumeUnit
    source: SourceProvenance
    adjustment_version: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        schema(self.schema_version)
        source(self.source)
        for field in ("security_id", "symbol", "venue", "product_id"):
            text(getattr(self, field), field)
        currency(self.currency)
        for field in ("bar_start_at", "bar_end_at", "available_at"):
            object.__setattr__(self, field, timestamp(getattr(self, field), field))
        if not self.bar_start_at < self.bar_end_at <= self.available_at <= self.source.ingested_at:
            fail(ErrorCode.TIME_ORDER, "bar_end_at", "Require start < end <= available <= ingested")
        for field in ("open", "high", "low", "close", "volume"):
            object.__setattr__(
                self, field, number(getattr(self, field), field, positive=field != "volume")
            )
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            fail(ErrorCode.INVALID_OHLCV, "ohlc", "Require low <= open/close <= high")
        enum_value(self.price_basis, PriceBasis, "price_basis")
        enum_value(self.share_unit, ShareUnit, "share_unit")
        enum_value(self.volume_unit, VolumeUnit, "volume_unit")
        if self.adjustment_version is not None:
            text(self.adjustment_version, "adjustment_version")


@dataclass(frozen=True, slots=True, kw_only=True)
class RawBar(_Bar):
    def __post_init__(self):
        _Bar.__post_init__(self)
        if (
            self.price_basis is not PriceBasis.RAW
            or self.share_unit is not ShareUnit.AS_TRADED
            or self.volume_unit is not VolumeUnit.AS_TRADED_SHARES
            or self.adjustment_version is not None
        ):
            fail(
                ErrorCode.UNIT_MISMATCH,
                "price_basis",
                "Raw bars require unadjusted prices and shares",
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalBar(_Bar):
    raw_product_id: str

    def __post_init__(self):
        _Bar.__post_init__(self)
        text(self.raw_product_id, "raw_product_id")
        if self.product_id == self.raw_product_id:
            fail(
                ErrorCode.UNIT_MISMATCH,
                "product_id",
                "Signal product must be separate from raw product",
            )
        if self.price_basis is PriceBasis.RAW:
            if (
                self.share_unit is not ShareUnit.AS_TRADED
                or self.volume_unit is not VolumeUnit.AS_TRADED_SHARES
                or self.adjustment_version is not None
            ):
                fail(ErrorCode.UNIT_MISMATCH, "share_unit", "Raw signal requires as-traded units")
        elif self.share_unit is not ShareUnit.SPLIT_ADJUSTED or self.adjustment_version is None:
            fail(
                ErrorCode.UNIT_MISMATCH,
                "adjustment_version",
                "Adjusted signal requires unit and version",
            )


def validate_price_risk_input(execution: RawBar, risk: RawBar | SignalBar) -> None:
    """Check declarations for ATR/absolute prices, not authenticity or calculated risk values."""
    if not isinstance(execution, RawBar) or not isinstance(risk, RawBar | SignalBar):
        fail(ErrorCode.INVALID_TYPE, "risk", "Expected raw execution and typed risk bar")
    if risk.available_at > execution.available_at:
        fail(
            ErrorCode.TIME_ORDER,
            "risk.available_at",
            "Risk input is not available with execution input",
        )
    if (
        risk.price_basis is not PriceBasis.RAW
        or risk.share_unit is not execution.share_unit
        or risk.security_id != execution.security_id
        or risk.symbol != execution.symbol
        or risk.currency != execution.currency
        or risk.venue != execution.venue
        or risk.bar_start_at != execution.bar_start_at
        or risk.bar_end_at != execution.bar_end_at
        or (isinstance(risk, SignalBar) and risk.raw_product_id != execution.product_id)
    ):
        fail(
            ErrorCode.UNIT_MISMATCH,
            "risk",
            "Price risk input must match execution identity, interval and units",
        )
