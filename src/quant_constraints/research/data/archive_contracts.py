"""Small-window acquisition and archive declarations, without truth certification."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Never
from zoneinfo import ZoneInfo

from .contracts import SCHEMA_VERSION
from .contracts.errors import timestamp
from .normalize import NORMALIZER_VERSION

ARCHIVE_VERSION = "research_archive_v1"
ADAPTER_VERSION = "massive_aggregates_v1"
ACQUISITION_VERSION = "research_acquisition_v1"
RECEIPT_VERSION = "research_receipt_v1"
EVIDENCE_ROLES = frozenset(
    {"corporate_actions", "adjustment_factors", "comparison", "source_definition", "license_terms"}
)
RETRY_STATUSES = frozenset({0, 429, 500, 502, 503, 504})
FEATURES = frozenset({"trf", "auction", "odd_lot", "out_of_session"})
ROLES = frozenset(
    {
        "request",
        "response",
        "receipt",
        "identities",
        "corporate_actions",
        "adjustment_factors",
        "comparison",
        "normalized_raw",
        "report",
        "source_manifest",
        "attempt_response",
        "source_definition",
        "license_terms",
        "acquisition_session",
    }
)


class ArchiveError(ValueError):
    def __init__(self, code: str, field: str, detail: str):
        self.code, self.field, self.detail = code, field, detail
        super().__init__(f"{code}:{field}: {detail}")

    def as_dict(self) -> dict[str, str]:
        return {
            "schema_version": ARCHIVE_VERSION,
            "code": self.code,
            "field": self.field,
            "detail": self.detail,
        }


def reject(code: str, field: str, detail: str) -> Never:
    raise ArchiveError(code, field, detail) from None


def nonempty(value: object, field: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        reject("INVALID_REQUEST", field, "Expected nonempty text without edge whitespace")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def wire(value: Any) -> Any:
    """Canonical JSON values; callers serialize public entries, never IdentityMap caches."""
    if isinstance(value, datetime):
        return timestamp(value, "timestamp").isoformat()
    if isinstance(value, dict):
        return {key: wire(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [wire(item) for item in value]
    return value


def canonical(value: Any) -> bytes:
    return json.dumps(
        wire(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def strict(cls: type, value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
        reject("UNSUPPORTED_MANIFEST", cls.__name__, "Expected exactly the versioned fields")
    return dict(value)


def parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        reject("UNSUPPORTED_MANIFEST", "timestamp", "Expected an ISO timestamp")
    try:
        return timestamp(datetime.fromisoformat(value), "timestamp")
    except ValueError:
        reject("UNSUPPORTED_MANIFEST", "timestamp", "Expected an aware ISO timestamp")


def strings(value: object, field: str) -> tuple[str, ...]:
    if (
        not isinstance(value, tuple)
        or not all(isinstance(item, str) for item in value)
        or len(set(value)) != len(value)
    ):
        reject("INVALID_REQUEST", field, "Expected a tuple of unique strings")
    for item in value:
        nonempty(item, field)
    return tuple(sorted(value))


@dataclass(frozen=True, slots=True, kw_only=True)
class CoverageDeclaration:
    market_scope: str
    source_feed: str
    definition_ref: str
    tapes: tuple[str, ...]
    venues: tuple[str, ...]
    included: tuple[str, ...]
    excluded: tuple[str, ...]
    unknown: tuple[str, ...]
    trade_condition_policy: str
    correction_policy: str
    quote_availability: str

    def __post_init__(self):
        for name in ("market_scope", "quote_availability"):
            nonempty(getattr(self, name), name)
        if self.market_scope not in {"us_consolidated", "single_venue"}:
            reject("INVALID_REQUEST", "market_scope", "Declare consolidated or single-venue scope")
        for name in (
            "source_feed",
            "definition_ref",
            "trade_condition_policy",
            "correction_policy",
        ):
            nonempty(getattr(self, name), name)
        for name in ("tapes", "venues", "included", "excluded", "unknown"):
            object.__setattr__(self, name, strings(getattr(self, name), name))
        if not set(self.tapes) <= {"A", "B", "C"}:
            reject("INVALID_REQUEST", "tapes", "Expected Tape A/B/C declarations")
        groups = tuple(set(getattr(self, name)) for name in ("included", "excluded", "unknown"))
        if set.union(*groups) != FEATURES or sum(map(len, groups)) != len(FEATURES):
            reject(
                "INVALID_REQUEST", "coverage", "Each coverage feature needs exactly one declaration"
            )
        if self.market_scope == "single_venue" and len(self.venues) != 1:
            reject("INVALID_REQUEST", "venues", "Single-venue scope needs one declared venue")
        if self.quote_availability not in {"not_collected", "unknown"}:
            reject("INVALID_REQUEST", "quote_availability", "This archive does not certify NBBO")

    @classmethod
    def from_dict(cls, value: Any) -> CoverageDeclaration:
        values = strict(cls, value)
        for name in ("tapes", "venues", "included", "excluded", "unknown"):
            if not isinstance(values[name], list):
                reject("UNSUPPORTED_MANIFEST", name, "Expected JSON array")
            values[name] = tuple(values[name])
        return cls(**values)


@dataclass(frozen=True, slots=True, kw_only=True)
class AccessDeclaration:
    mode: str
    plan: str
    license_ref: str
    archive_allowed: bool
    history_start_at: datetime
    history_end_at: datetime
    frequencies: tuple[str, ...]

    def __post_init__(self):
        nonempty(self.mode, "mode")
        if self.mode not in {"synthetic", "licensed_private"} or self.archive_allowed is not True:
            reject(
                "UNAUTHORIZED",
                "access",
                "Explicit synthetic or licensed private archival permission required",
            )
        for name in ("plan", "license_ref"):
            nonempty(getattr(self, name), name)
        for name in ("history_start_at", "history_end_at"):
            object.__setattr__(self, name, timestamp(getattr(self, name), name))
        object.__setattr__(self, "frequencies", strings(self.frequencies, "frequencies"))
        if (
            self.history_start_at >= self.history_end_at
            or not self.frequencies
            or not set(self.frequencies) <= {"1m", "1d"}
        ):
            reject("INVALID_REQUEST", "access", "Invalid declared permission range/frequency")

    @classmethod
    def from_dict(cls, value: Any) -> AccessDeclaration:
        values = strict(cls, value)
        for name in ("history_start_at", "history_end_at"):
            values[name] = parse_time(values[name])
        if not isinstance(values["frequencies"], list):
            reject("UNSUPPORTED_MANIFEST", "frequencies", "Expected JSON array")
        values["frequencies"] = tuple(values["frequencies"])
        return cls(**values)


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRequest:
    dataset_id: str
    security_id: str
    symbol: str
    venue: str
    currency: str
    start_at: datetime
    end_at: datetime
    frequency: str
    session_filter: str
    coverage: CoverageDeclaration
    access: AccessDeclaration
    page_limit: int = 50000
    provider: str = "massive"
    api_version: str = "v2"
    adjusted: bool = False
    schema_version: str = SCHEMA_VERSION
    normalizer_version: str = NORMALIZER_VERSION

    def __post_init__(self):
        for name in (
            "dataset_id",
            "security_id",
            "symbol",
            "venue",
            "currency",
            "provider",
            "api_version",
            "frequency",
            "session_filter",
            "schema_version",
            "normalizer_version",
        ):
            nonempty(getattr(self, name), name)
        if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,15}", self.symbol) is None:
            reject("INVALID_REQUEST", "symbol", "Unsupported stock ticker")
        if self.currency != "USD" or self.provider != "massive" or self.api_version != "v2":
            reject(
                "INVALID_REQUEST",
                "provider",
                "Only the declared USD Massive aggregate API is supported",
            )
        if self.schema_version != SCHEMA_VERSION or self.normalizer_version != NORMALIZER_VERSION:
            reject("UNSUPPORTED_MANIFEST", "schema_version", "Only the 5A v2 contract is supported")
        if self.adjusted is not False or self.frequency not in {"1m", "1d"}:
            reject(
                "INVALID_REQUEST",
                "adjusted",
                "This raw acquisition requires adjusted=false and 1m/1d",
            )
        if self.session_filter not in {"rth", "all_source_sessions"} or (
            self.frequency == "1d" and self.session_filter == "rth"
        ):
            reject(
                "INVALID_REQUEST", "session_filter", "Daily aggregates cannot be relabeled RTH-only"
            )
        if type(self.page_limit) is not int or not 1 <= self.page_limit <= 50000:
            reject("INVALID_REQUEST", "page_limit", "Expected 1..50000 base aggregates")
        if not isinstance(self.coverage, CoverageDeclaration) or not isinstance(
            self.access, AccessDeclaration
        ):
            reject(
                "INVALID_REQUEST",
                "declarations",
                "Expected immutable coverage and access declarations",
            )
        for name in ("start_at", "end_at"):
            stamp = timestamp(getattr(self, name), name)
            local = stamp.astimezone(ZoneInfo("America/New_York"))
            if (
                stamp.microsecond
                or stamp.second
                or (self.frequency == "1d" and (local.hour or local.minute))
            ):
                reject("INVALID_REQUEST", name, "Use exact minute boundaries")
            object.__setattr__(self, name, stamp)
        if not timedelta(0) < self.end_at - self.start_at <= timedelta(days=7):
            reject("INVALID_REQUEST", "range", "Acquisition is limited to seven days, half-open")
        if (
            self.start_at < self.access.history_start_at
            or self.end_at > self.access.history_end_at
            or self.frequency not in self.access.frequencies
        ):
            reject(
                "UNAUTHORIZED",
                "range",
                "Requested history/frequency is outside declared permission",
            )

    @property
    def request_id(self) -> str:
        return digest(canonical(asdict(self)))

    @classmethod
    def from_dict(cls, value: Any) -> SourceRequest:
        values = strict(cls, value)
        values["coverage"] = CoverageDeclaration.from_dict(values["coverage"])
        values["access"] = AccessDeclaration.from_dict(values["access"])
        for name in ("start_at", "end_at"):
            values[name] = parse_time(values[name])
        return cls(**values)


@dataclass(frozen=True, slots=True, kw_only=True)
class ArchiveFile:
    path: str
    role: str
    sha256: str
    size_bytes: int
    source_ref: str

    def __post_init__(self):
        nonempty(self.role, "role")
        if (
            not isinstance(self.path, str)
            or not self.path
            or "\\" in self.path
            or ":" in self.path
            or "\x00" in self.path
        ):
            reject("PATH_INVALID", "path", "Expected canonical relative POSIX path")
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != self.path
            or self.path in {".", "manifest.json", "manifest.sha256"}
        ):
            reject("PATH_INVALID", "path", "Archive paths must be canonical and relative")
        if self.role not in ROLES or type(self.size_bytes) is not int or self.size_bytes < 0:
            reject("UNSUPPORTED_MANIFEST", "file", "Invalid file role or length")
        if not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            reject("UNSUPPORTED_MANIFEST", "sha256", "Expected SHA-256 hexadecimal digest")
        nonempty(self.source_ref, "source_ref")


def acquisition_binding(
    request: SourceRequest, acquisition_id: str, identity_sha256: str, evidence_sha256: str
) -> bytes:
    """Bind one acquisition namespace, without upgrading the market-data contract."""
    if (
        not isinstance(acquisition_id, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", acquisition_id) is None
    ):
        reject("INVALID_REQUEST", "acquisition_id", "Use a bounded portable acquisition identifier")
    return canonical(
        {
            "schema_version": ACQUISITION_VERSION,
            "request_id": request.request_id,
            "acquisition_id": acquisition_id,
            "request_sha256": digest(canonical(asdict(request))),
            "identities_sha256": identity_sha256,
            "evidence_sha256": evidence_sha256,
        }
    )


def evidence_fingerprint(files: tuple[ArchiveFile, ...]) -> bytes:
    return canonical(
        [
            {
                "path": item.path,
                "role": item.role,
                "sha256": item.sha256,
                "source_ref": item.source_ref,
            }
            for item in sorted(files, key=lambda item: item.path)
            if item.role in EVIDENCE_ROLES
        ]
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ArchiveManifest:
    request: SourceRequest
    layer: str
    files: tuple[ArchiveFile, ...]
    acquired_start_at: datetime
    acquired_end_at: datetime
    adapter_version: str
    writer_version: str
    source_manifest_sha256: str | None
    schema_version: str = ARCHIVE_VERSION

    def __post_init__(self):
        nonempty(self.layer, "layer")
        if self.schema_version != ARCHIVE_VERSION or not isinstance(self.request, SourceRequest):
            reject("UNSUPPORTED_MANIFEST", "schema_version", "Unsupported archive contract")
        if (
            self.layer not in {"source", "normalized"}
            or not isinstance(self.files, tuple)
            or not all(isinstance(item, ArchiveFile) for item in self.files)
        ):
            reject("UNSUPPORTED_MANIFEST", "files", "Expected immutable versioned file records")
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)) or not self.files:
            reject("UNSUPPORTED_MANIFEST", "files", "Duplicate paths or empty manifest")
        object.__setattr__(self, "files", tuple(sorted(self.files, key=lambda item: item.path)))
        roles = {item.role for item in self.files}
        for role in ("request", "identities"):
            if sum(item.role == role for item in self.files) != 1:
                reject(
                    "UNSUPPORTED_MANIFEST",
                    "files",
                    "One canonical request and identity file required",
                )
        if (
            not {"request", "identities"} <= roles
            or (self.layer == "source" and not {"response", "receipt"} <= roles)
            or (self.layer == "normalized" and not {"report", "source_manifest"} <= roles)
        ):
            reject("UNSUPPORTED_MANIFEST", "files", "Required evidence files are absent")
        for name in ("adapter_version", "writer_version"):
            nonempty(getattr(self, name), name)
        for name in ("acquired_start_at", "acquired_end_at"):
            object.__setattr__(self, name, timestamp(getattr(self, name), name))
        if self.acquired_end_at < self.acquired_start_at:
            reject("UNSUPPORTED_MANIFEST", "acquired_end_at", "Acquisition clocks are inverted")
        parent = self.source_manifest_sha256
        if (self.layer == "source" and parent is not None) or (
            self.layer == "normalized"
            and (not isinstance(parent, str) or re.fullmatch(r"[0-9a-f]{64}", parent) is None)
        ):
            reject(
                "UNSUPPORTED_MANIFEST",
                "source_manifest_sha256",
                "Normalized archives must bind their source manifest",
            )

    @property
    def revision(self) -> str:
        return digest(canonical(asdict(self)))

    @classmethod
    def from_dict(cls, value: Any) -> ArchiveManifest:
        values = strict(cls, value)
        values["request"] = SourceRequest.from_dict(values["request"])
        if not isinstance(values["files"], list):
            reject("UNSUPPORTED_MANIFEST", "files", "Expected file array")
        values["files"] = tuple(
            ArchiveFile(**strict(ArchiveFile, item)) for item in values["files"]
        )
        for name in ("acquired_start_at", "acquired_end_at"):
            values[name] = parse_time(values[name])
        return cls(**values)
