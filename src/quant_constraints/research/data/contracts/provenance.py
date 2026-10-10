"""Source declarations, deliberately without a verified flag or trust token."""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import SCHEMA_VERSION, ErrorCode, fail, schema, text, timestamp, version


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceProvenance:
    provider: str
    dataset_id: str
    dataset_revision: str
    record_id: str
    record_version: int
    ingested_at: datetime
    source_uri: str
    source_timezone: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        schema(self.schema_version)
        for field in (
            "provider",
            "dataset_id",
            "dataset_revision",
            "record_id",
            "source_uri",
            "source_timezone",
        ):
            text(getattr(self, field), field)
        version(self.record_version, "record_version")
        object.__setattr__(self, "ingested_at", timestamp(self.ingested_at, "ingested_at"))
        try:
            ZoneInfo(self.source_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            fail(ErrorCode.INVALID_VALUE, "source_timezone", "Expected IANA timezone")


def source(value: object) -> None:
    if not isinstance(value, SourceProvenance):
        fail(ErrorCode.INVALID_TYPE, "source", "Expected SourceProvenance")
