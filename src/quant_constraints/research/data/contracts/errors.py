"""Versioned, deterministic contract diagnostics."""

import math
import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Never

SCHEMA_VERSION = "research_data_v1"


class ErrorCode(StrEnum):
    MISSING_FIELD = "MISSING_FIELD"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    INVALID_TYPE = "INVALID_TYPE"
    INVALID_VALUE = "INVALID_VALUE"
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
    NAIVE_TIMESTAMP = "NAIVE_TIMESTAMP"
    TIME_ORDER = "TIME_ORDER"
    INVALID_OHLCV = "INVALID_OHLCV"
    UNIT_MISMATCH = "UNIT_MISMATCH"
    DUPLICATE_BAR = "DUPLICATE_BAR"
    OVERLAPPING_BAR = "OVERLAPPING_BAR"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    IDENTITY_MISSING = "IDENTITY_MISSING"
    UNSUPPORTED_IDENTITY = "UNSUPPORTED_IDENTITY"
    UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"
    ACTION_TERMS = "ACTION_TERMS"
    KNOWLEDGE_EVIDENCE = "KNOWLEDGE_EVIDENCE"
    CREDIT_EVIDENCE = "CREDIT_EVIDENCE"


class ContractError(ValueError):
    def __init__(self, code: ErrorCode, field: str, detail: str):
        self.code, self.field, self.detail = code, field, detail
        super().__init__(f"{code.value}:{field}: {detail}")

    def as_dict(self) -> dict[str, str]:
        return {
            "schema_version": SCHEMA_VERSION,
            "code": self.code.value,
            "field": self.field,
            "detail": self.detail,
        }


def fail(code: ErrorCode, field: str, detail: str) -> Never:
    raise ContractError(code, field, detail)


def text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        fail(ErrorCode.INVALID_VALUE, field, "Expected nonempty text without edge whitespace")


def schema(value: object) -> None:
    if value != SCHEMA_VERSION:
        fail(ErrorCode.UNSUPPORTED_SCHEMA, "schema_version", "Unsupported schema version")


def timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        fail(ErrorCode.INVALID_TYPE, field, "Expected datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        fail(ErrorCode.NAIVE_TIMESTAMP, field, "Timezone is required")
    return value.astimezone(UTC)


def day(value: object, field: str) -> None:
    if type(value) is not date:
        fail(ErrorCode.INVALID_TYPE, field, "Expected date, not datetime")


def number(value: object, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        fail(ErrorCode.INVALID_TYPE, field, "Expected finite numeric value")
    try:
        result = float(value)
    except OverflowError:
        fail(ErrorCode.INVALID_VALUE, field, "Numeric value is out of range")
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        fail(ErrorCode.INVALID_VALUE, field, "Expected finite nonnegative or positive value")
    return result


def version(value: object, field: str) -> None:
    if type(value) is not int or value < 1:
        fail(ErrorCode.INVALID_VALUE, field, "Expected positive integer version")


def currency(value: object) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Z]{3}", value) is None:
        fail(ErrorCode.INVALID_VALUE, "currency", "Expected uppercase three-letter currency")


def enum_value(value: object, cls: type[StrEnum], field: str) -> None:
    if not isinstance(value, cls):
        fail(ErrorCode.INVALID_VALUE, field, f"Expected {cls.__name__} member")
