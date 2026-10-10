"""Bounded Massive aggregate acquisition; injected transports make CI fully offline."""

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .archive import (
    ArchivedSnapshot,
    ArchiveInput,
    ArchiveStore,
    load_archive,
    safe_path,
    write_once,
)
from .archive_contracts import (
    ACQUISITION_VERSION,
    ADAPTER_VERSION,
    EVIDENCE_ROLES,
    RECEIPT_VERSION,
    RETRY_STATUSES,
    ArchiveFile,
    SourceRequest,
    acquisition_binding,
    canonical,
    digest,
    evidence_fingerprint,
    reject,
)
from .contracts import IdentityMap
from .contracts.errors import timestamp
from .pagination import (
    CUSTOM_BARS_SPEC,
    PAGINATION_VERSION,
    PaginationChain,
    pagination_contract,
)
from .pagination import (
    checked_url as checked_url,
)
from .pagination import (
    parse_page as parse_page,
)
from .pagination import (
    request_url as request_url,
)

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ACQUISITION_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status: int
    body: bytes
    headers: tuple[tuple[str, str], ...] = ()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_get(url: str, headers: dict[str, str], timeout: float) -> HTTPResponse:
    """Fixed HTTPS provider origin; redirects never receive the bearer credential."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "api.massive.com":
        reject("PAGE_INVALID", "url", "Native transport requires the fixed HTTPS provider origin")
    request = Request(url, headers=headers, method="GET")
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            return HTTPResponse(
                response.status,
                response.read(MAX_RESPONSE_BYTES + 1),
                tuple(response.headers.items()),
            )
    except HTTPError as error:
        with error:
            return HTTPResponse(
                error.code, error.read(MAX_RESPONSE_BYTES + 1), tuple(error.headers.items())
            )


class MassiveAdapter:
    """No key is stored in requests, repr, manifests, receipts or error messages."""

    def __init__(
        self,
        *,
        transport: Callable[[str, dict[str, str], float], HTTPResponse] | None = None,
        api_key: str | None = None,
        retries: int = 3,
        max_pages: int = 32,
        timeout: float = 20,
        minimum_interval: float = 12,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if (
            type(retries) is not int
            or not 0 <= retries <= 8
            or type(max_pages) is not int
            or not 1 <= max_pages <= 100
        ):
            reject("INVALID_REQUEST", "retry", "Retries/pages must have finite bounds")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not 0 < timeout <= 60
            or isinstance(minimum_interval, bool)
            or not isinstance(minimum_interval, int | float)
            or not 0 <= minimum_interval <= 60
        ):
            reject("INVALID_REQUEST", "timeout", "Timeout/interval must have finite bounds")
        self._native = transport is None
        self._transport = transport or http_get
        self._key = (
            api_key
            if api_key is not None
            else (os.environ.get("MASSIVE_API_KEY") if self._native else None)
        )
        if self._key is not None and (
            not isinstance(self._key, str)
            or not self._key
            or any(char.isspace() for char in self._key)
        ):
            reject("INVALID_REQUEST", "api_key", "Invalid credential")
        self._retries, self._max_pages = retries, max_pages
        self._timeout, self._interval = timeout, minimum_interval
        self._clock, self._sleep = clock, sleeper

    def _check_secret(self, content: bytes) -> None:
        if self._key and self._key.encode() in content:
            reject(
                "KEY_EXPOSURE",
                "response",
                "Provider content contains a credential; original bytes cannot be safely archived",
            )
        if re.search(
            rb"(?i)(?:apiKey|api_key|access_token|authorization)\s*(?:=|[\"]\s*:)", content
        ):
            reject("KEY_EXPOSURE", "response", "Credential-bearing content cannot be archived")

    def _fetch(
        self, url: str, request: SourceRequest
    ) -> tuple[HTTPResponse, tuple[dict, ...], tuple[bytes, ...]]:
        url = checked_url(url, request)
        headers = {"Accept": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        attempts = []
        bodies = []
        for attempt in range(self._retries + 1):
            self._sleep(self._interval)
            started = timestamp(self._clock(), "request_start_at")
            if request.end_at > started:
                reject(
                    "TIME_ORDER",
                    "request_end_at",
                    "Historical acquisition requires a completed request window",
                )
            try:
                response = self._transport(url, headers, self._timeout)
            except (TimeoutError, URLError, OSError):
                response = HTTPResponse(0, b"")
            ended = timestamp(self._clock(), "request_end_at")
            if ended < started:
                reject("TIME_ORDER", "request_end_at", "Acquisition clock moved backwards")
            if (
                not isinstance(response, HTTPResponse)
                or type(response.status) is not int
                or not isinstance(response.body, bytes)
                or len(response.body) > MAX_RESPONSE_BYTES
            ):
                reject("PAGE_INVALID", "response", "Invalid or oversized HTTP response")
            self._check_secret(response.body)
            bodies.append(response.body)
            attempts.append(
                {
                    "ordinal": attempt,
                    "status": response.status,
                    "started_at": started.isoformat(),
                    "ended_at": ended.isoformat(),
                    "response_sha256": digest(response.body),
                }
            )
            if response.status == 200:
                return response, tuple(attempts), tuple(bodies)
            if response.status in {401, 403}:
                reject(
                    "PERMISSION_GAP",
                    "status",
                    "Provider denied access; this is not empty market data",
                )
            if response.status not in RETRY_STATUSES:
                reject("TRANSPORT_REJECTED", "status", "Unexpected response or redirect")
            if attempt < self._retries:
                delay = 0.5 * 2**attempt
                try:
                    declared = float(
                        {key.lower(): value for key, value in response.headers}.get(
                            "retry-after", "0"
                        )
                    )
                    if 0 < declared <= 60:
                        delay = max(delay, declared)
                except (ValueError, TypeError):
                    pass
                self._sleep(min(60.0, delay))
        reject("RETRY_EXHAUSTED", "status", "Bounded acquisition attempts exhausted")

    def acquire_and_archive(
        self,
        request: SourceRequest,
        store: ArchiveStore,
        identities: IdentityMap,
        *,
        acquisition_id: str = "initial",
        evidence: tuple[ArchiveInput, ...] = (),
    ) -> ArchivedSnapshot:
        if not isinstance(request, SourceRequest) or not isinstance(identities, IdentityMap):
            reject("INVALID_REQUEST", "request", "Expected request and historical identity map")
        if request.coverage.market_scope != "us_consolidated":
            reject(
                "UNSUPPORTED_SCOPE",
                "market_scope",
                "The aggregate endpoint cannot request single-venue coverage",
            )
        if self._native and (request.access.mode != "licensed_private" or not self._key):
            reject(
                "UNAUTHORIZED",
                "access",
                "Native HTTP requires private archive permission and an environment credential",
            )
        if (
            not isinstance(acquisition_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", acquisition_id) is None
        ):
            reject(
                "INVALID_REQUEST", "acquisition_id", "Use a bounded portable acquisition identifier"
            )
        identity = identities.resolve_effective(request.symbol, request.venue, request.start_at)
        if (
            identity.security_id != request.security_id
            or identity.currency != request.currency
            or (identity.effective_until is not None and request.end_at > identity.effective_until)
        ):
            reject(
                "IDENTITY_CONFLICT", "range", "Split a request at historical identity boundaries"
            )
        metadata = canonical([asdict(item) for item in identities.entries])
        for item in evidence:
            if item.role not in EVIDENCE_ROLES:
                reject(
                    "INVALID_REQUEST",
                    "evidence",
                    "Only explicit supplementary evidence roles are allowed",
                )
            self._check_secret(item.content)
        request_bytes = canonical(asdict(request))
        self._check_secret(request_bytes)
        self._check_secret(metadata)
        session = safe_path(store.root, f"sessions/{request.request_id}/{acquisition_id}")
        session.mkdir(parents=True, exist_ok=True)
        write_once(session / "request.json", request_bytes)
        write_once(session / "identities.json", metadata)
        fingerprint = evidence_fingerprint(
            tuple(
                ArchiveFile(
                    path=item.path,
                    role=item.role,
                    sha256=digest(item.content),
                    size_bytes=len(item.content),
                    source_ref=item.source_ref,
                )
                for item in evidence
            )
        )
        self._check_secret(fingerprint)
        write_once(session / "evidence.json", fingerprint)
        binding = acquisition_binding(
            request, acquisition_id, digest(metadata), digest(fingerprint)
        )
        write_once(session / "acquisition.json", binding)
        paging = pagination_contract(request)
        write_once(session / "pagination.json", paging)
        checkpoint_binding = {
            "schema_version": ACQUISITION_VERSION,
            "request_id": request.request_id,
            "acquisition_id": acquisition_id,
            "binding_sha256": digest(binding),
        }
        completed = safe_path(session, "completed.json")
        if completed.exists():
            try:
                checkpoint_bytes = completed.read_bytes()
                checkpoint = json.loads(checkpoint_bytes)
                revision = checkpoint["revision"]
            except (ValueError, KeyError, OSError, TypeError):
                reject("HASH_MISMATCH", "checkpoint", "Invalid acquisition checkpoint")
            if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{64}", revision) is None:
                reject("HASH_MISMATCH", "checkpoint", "Invalid acquisition revision")
            if checkpoint_bytes != canonical(checkpoint_binding | {"revision": revision}):
                reject("SOURCE_CONFLICT", "checkpoint", "Checkpoint belongs to another acquisition")
            snapshot = load_archive(safe_path(store.root, "source/" + revision))
            if (
                snapshot.manifest.request != request
                or snapshot.manifest.adapter_version != ADAPTER_VERSION
                or {
                    item.path
                    for item in snapshot.manifest.files
                    if item.role == "acquisition_session"
                }
                != {"acquisition.json"}
                or snapshot.read("acquisition.json") != binding
                or snapshot.read("identities.json") != metadata
                or evidence_fingerprint(snapshot.manifest.files) != fingerprint
            ):
                reject(
                    "SOURCE_CONFLICT", "checkpoint", "Archive differs from the current acquisition"
                )
            try:
                for item in snapshot.manifest.files:
                    if item.role in {"response", "receipt", "attempt_response"}:
                        content = safe_path(session, item.path).read_bytes()
                        if len(content) != item.size_bytes or digest(content) != item.sha256:
                            reject(
                                "HASH_MISMATCH",
                                "checkpoint",
                                "Archive differs from saved session pages",
                            )
                        if (
                            item.role == "receipt"
                            and safe_path(session, item.path.removesuffix(".json") + ".sha256")
                            .read_text(encoding="ascii")
                            .strip()
                            != item.sha256
                        ):
                            reject("HASH_MISMATCH", "checkpoint", "Saved receipt digest differs")
            except (OSError, UnicodeError):
                reject("HASH_MISMATCH", "checkpoint", "Completed session pages are unreadable")
            return snapshot
        inputs = [
            ArchiveInput(
                "request.json", "request", request_bytes, "request://" + request.request_id
            ),
            ArchiveInput("identities.json", "identities", metadata, "identity-map://canonical"),
            ArchiveInput(
                "acquisition.json",
                "acquisition_session",
                binding,
                f"acquisition://{request.request_id}/{acquisition_id}",
            ),
            ArchiveInput("pagination.json", "pagination_contract", paging, CUSTOM_BARS_SPEC),
            *evidence,
        ]
        chain = PaginationChain(request)
        url, visited, total = request_url(request), set(), 0
        first, last = None, None
        for index in range(self._max_pages):
            if url in visited:
                reject("PAGINATION_LOOP", "next_url", "Repeated pagination cursor")
            visited.add(url)
            prefix = f"pages/{index:04d}"
            receipt: dict[str, Any]
            response_path, receipt_path = (
                safe_path(session, prefix + ".json"),
                safe_path(session, prefix + ".receipt.json"),
            )
            if receipt_path.exists():
                try:
                    receipt_bytes = receipt_path.read_bytes()
                    receipt = json.loads(receipt_bytes)
                    if safe_path(session, prefix + ".receipt.sha256").read_text(
                        encoding="ascii"
                    ).strip() != digest(receipt_bytes):
                        reject("HASH_MISMATCH", "checkpoint", "Receipt bytes changed")
                    body = safe_path(session, prefix + ".json").read_bytes()
                    if receipt["url"] != url or receipt["sha256"] != digest(body):
                        reject("HASH_MISMATCH", "checkpoint", "Checkpoint response changed")
                except (OSError, ValueError, KeyError, TypeError, IndexError):
                    reject("HASH_MISMATCH", "checkpoint", "Incomplete or invalid saved page")
                chain.accept(url, parse_page(body, request))
            else:
                if response_path.exists():
                    reject(
                        "HASH_MISMATCH",
                        "checkpoint",
                        "Uncommitted response needs a new acquisition ID",
                    )
                response, attempts, bodies = self._fetch(url, request)
                body = response.body
                page = parse_page(body, request)
                chain.accept(url, page)
                safe_headers = tuple(
                    sorted(
                        (key.lower(), value)
                        for key, value in response.headers
                        if key.lower() in {"content-type", "date", "x-request-id"}
                    )
                )
                receipt = {
                    "schema_version": RECEIPT_VERSION,
                    "pagination_version": PAGINATION_VERSION,
                    "url": url,
                    "sha256": digest(body),
                    "status": 200,
                    "attempts": attempts,
                    "provider_request_id": page["request_id"],
                    "headers": safe_headers,
                    "next_url": page.get("next_url"),
                    "record_id_kind": "derived_page_row_timestamp_v1",
                    "retry_responses": [
                        {
                            "ordinal": ordinal,
                            "status": attempts[ordinal]["status"],
                            "path": f"{prefix}.attempt-{ordinal:02d}.bin",
                            "sha256": digest(content),
                        }
                        for ordinal, content in enumerate(bodies[:-1])
                    ],
                }
                receipt_bytes = canonical(receipt)
                self._check_secret(receipt_bytes)
                for entry, content in zip(receipt["retry_responses"], bodies[:-1], strict=True):
                    write_once(safe_path(session, entry["path"]), content)
                write_once(response_path, body)
                write_once(receipt_path, receipt_bytes)
                write_once(
                    session / (prefix + ".receipt.sha256"),
                    (digest(receipt_bytes) + "\n").encode("ascii"),
                )
            page = parse_page(body, request)
            total += len(body)
            try:
                for entry in receipt["retry_responses"]:
                    path = entry["path"]
                    if not isinstance(path, str) or not re.fullmatch(
                        re.escape(prefix) + r"\.attempt-[0-9]{2}\.bin", path
                    ):
                        reject(
                            "HASH_MISMATCH",
                            "checkpoint",
                            "Retry response path differs from its page",
                        )
                    content = safe_path(session, path).read_bytes()
                    if digest(content) != entry["sha256"]:
                        reject("HASH_MISMATCH", "checkpoint", "Retry response bytes changed")
                    total += len(content)
                    inputs.append(ArchiveInput(path, "attempt_response", content, url))
                started = timestamp(
                    datetime.fromisoformat(receipt["attempts"][0]["started_at"]), "started_at"
                )
                ended = timestamp(
                    datetime.fromisoformat(receipt["attempts"][-1]["ended_at"]), "ended_at"
                )
                if (
                    receipt["provider_request_id"] != page["request_id"]
                    or receipt["next_url"] != page.get("next_url")
                    or ended < started
                ):
                    reject(
                        "HASH_MISMATCH",
                        "checkpoint",
                        "Receipt semantics differ from original response",
                    )
            except (ValueError, OSError, TypeError, KeyError, IndexError):
                reject("HASH_MISMATCH", "checkpoint", "Invalid receipt or retry response")
            if total > MAX_ACQUISITION_BYTES:
                reject("PAGE_INVALID", "size", "Small-window acquisition exceeded its byte budget")
            first = started if first is None else first
            if last is not None and started < last:
                reject("TIME_ORDER", "pages", "Page acquisition clocks are not monotonic")
            last = ended
            inputs.extend(
                (
                    ArchiveInput(prefix + ".json", "response", body, url),
                    ArchiveInput(prefix + ".receipt.json", "receipt", receipt_bytes, url),
                )
            )
            if page.get("next_url") is None:
                snapshot = store.commit(
                    request=request,
                    layer="source",
                    inputs=tuple(inputs),
                    acquired_start_at=first,
                    acquired_end_at=last,
                    adapter_version=ADAPTER_VERSION,
                    writer_version="original_bytes_v1",
                )
                write_once(
                    completed,
                    canonical(checkpoint_binding | {"revision": snapshot.manifest.revision}),
                )
                return snapshot
            url = page["next_url"]
        reject(
            "PAGINATION_INCOMPLETE", "max_pages", "Pagination did not finish within the page bound"
        )
