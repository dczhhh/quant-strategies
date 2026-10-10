"""Versioned, conservative Massive aggregate pagination; no raw/PIT certification."""

import json
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from .archive_contracts import SourceRequest, canonical, digest, reject

PAGINATION_VERSION = "massive_aggregate_pagination_v1"
CUSTOM_BARS_SPEC = "https://massive.com/docs/rest/stocks/aggregates/custom-bars"
CURSOR_SPEC = "https://massive.com/blog/api-pagination-patterns"
NY = ZoneInfo("America/New_York")


def pagination_contract(request: SourceRequest) -> bytes:
    return canonical(
        {
            "schema_version": PAGINATION_VERSION,
            "request_sha256": digest(canonical(asdict(request))),
            "provider_specs": [CUSTOM_BARS_SPEC, CURSOR_SPEC],
            "query_fields": ["adjusted", "cursor", "limit", "sort"],
            "range_policy": "fixed_end_same_start_or_previous_bar_end",
            "opaque_cursor_policy": "synthetic_only_pending_licensed_raw_smoke",
            "termination_policy": "successful_no_next_below_base_limit_zero_count_if_empty",
            "market_data_verified": False,
        }
    )


def request_url(request: SourceRequest) -> str:
    span = "minute" if request.frequency == "1m" else "day"
    first = int(request.start_at.timestamp() * 1000)
    last = int(request.end_at.timestamp() * 1000) - 1
    return (
        f"https://api.massive.com/v2/aggs/ticker/{request.symbol}/range/1/{span}/{first}/{last}?"
        + urlencode({"adjusted": "false", "sort": "asc", "limit": request.page_limit})
    )


def _endpoint_time(value: str, *, inclusive_end: bool) -> int:
    if re.fullmatch(r"[0-9]{1,16}", value):
        return int(value)
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            at = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=NY)
            if inclusive_end:
                at += timedelta(days=1)
            return int(at.timestamp() * 1000) - int(inclusive_end)
        except (ValueError, OverflowError, OSError):
            pass
    reject("PAGE_INVALID", "next_url", "Unsupported aggregate range boundary")


def url_scope(url: str, request: SourceRequest) -> tuple[int, int, dict[str, str]]:
    if (
        not isinstance(url, str)
        or not 1 <= len(url) <= 4096
        or any(ord(c) <= 32 or ord(c) == 127 for c in url)
        or re.search(r"%(?![0-9A-Fa-f]{2})", url)
    ):
        reject("PAGE_INVALID", "next_url", "Expected a bounded canonical provider URL")
    try:
        parsed = urlsplit(url)
        pairs = parse_qsl(
            parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8
        )
    except ValueError:
        reject("PAGE_INVALID", "next_url", "Malformed provider URL/query")
    span = "minute" if request.frequency == "1m" else "day"
    prefix = f"/v2/aggs/ticker/{request.symbol}/range/1/{span}/"
    boundaries = parsed.path.removeprefix(prefix).split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc != "api.massive.com"
        or parsed.fragment
        or not parsed.path.startswith(prefix)
        or len(boundaries) != 2
    ):
        reject("PAGE_INVALID", "next_url", "Pagination changed the exact provider endpoint")
    if any(k.lower() in {"apikey", "api_key", "token", "access_token"} for k, _ in pairs):
        reject("KEY_EXPOSURE", "next_url", "Credential-bearing pagination cannot be archived")
    query = dict(pairs)
    if len(query) != len(pairs) or set(query) - {"adjusted", "sort", "limit", "cursor"}:
        reject("PAGE_INVALID", "next_url", "Unknown or repeated aggregate query parameter")
    expected = {"adjusted": "false", "sort": "asc", "limit": str(request.page_limit)}
    if any(k in query and query[k] != v for k, v in expected.items()):
        reject("PAGE_INVALID", "next_url", "Pagination changed raw/order/base-limit semantics")
    cursor = query.get("cursor")
    if cursor is not None:
        if re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,2048}", cursor) is None:
            reject("PAGE_INVALID", "next_url", "Malformed opaque cursor")
    elif any(query.get(k) != v for k, v in expected.items()):
        reject("PAGE_INVALID", "next_url", "Noncursor continuation requires explicit semantics")
    first, last = (
        _endpoint_time(boundaries[0], inclusive_end=False),
        _endpoint_time(boundaries[1], inclusive_end=True),
    )
    base_first, base_last = (
        int(request.start_at.timestamp() * 1000),
        int(request.end_at.timestamp() * 1000) - 1,
    )
    if not base_first <= first <= last or last != base_last:
        reject("PAGE_INVALID", "next_url", "Pagination range escaped or shortened the request")
    at = datetime.fromtimestamp(first / 1000, UTC)
    local = at.astimezone(NY)
    if first % 60000 or (request.frequency == "1d" and (local.hour or local.minute)):
        reject("PAGE_INVALID", "next_url", "Continuation does not align to the aggregate")
    return first, last, query


def checked_url(url: str, request: SourceRequest) -> str:
    url_scope(url, request)
    return urlunsplit(urlsplit(url))


def parse_page(body: bytes, request: SourceRequest, *, legacy: bool = False) -> dict:
    try:
        page = json.loads(
            body,
            parse_constant=lambda _: reject("PAGE_INVALID", "response", "Nonfinite JSON constant"),
        )
    except (ValueError, UnicodeError):
        reject("PAGE_INVALID", "response", "Provider response is not valid JSON")
    if not isinstance(page, dict) or page.get("status") not in {"OK", "DELAYED"}:
        reject("PAGE_INVALID", "status", "Provider did not acknowledge a successful response")
    if page.get("ticker") != request.symbol or page.get("adjusted") is not False:
        reject("PAGE_INVALID", "ticker", "Response ticker/raw declaration differs from request")
    rows = page.get("results", [])
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        reject("PAGE_INVALID", "results", "Expected provider record objects")
    if type(page.get("resultsCount")) is not int or page["resultsCount"] != len(rows):
        reject("PAGE_INVALID", "resultsCount", "Response count disagrees with records")
    if type(page.get("queryCount")) is not int or page["queryCount"] < 0:
        reject("PAGE_INVALID", "queryCount", "Base aggregate count is missing or invalid")
    if not isinstance(page.get("request_id"), str) or not page["request_id"]:
        reject("PAGE_INVALID", "request_id", "Provider request identity is missing")
    if legacy:
        # Offline decoding only: never follow or certify a pre-v2 pagination chain.
        return page
    if not len(rows) <= page["queryCount"] <= request.page_limit:
        reject("PAGE_INVALID", "queryCount", "Counts escape the multiplier-one base limit")
    if "next_url" in page and page["next_url"] is None:
        reject(
            "PAGINATION_INCOMPLETE", "next_url", "Null continuation is not documented exhaustion"
        )
    if page.get("next_url") is not None:
        checked_url(page["next_url"], request)
    elif page["queryCount"] >= request.page_limit:
        reject("PAGINATION_INCOMPLETE", "queryCount", "Base limit reached without continuation")
    return page


class PaginationChain:
    """Bind original URLs and monotonically ordered bars without assuming contiguous trades."""

    def __init__(self, request: SourceRequest):
        self.request = request
        self.next_url: str | None = request_url(request)
        self.last_stamp: int | None = None
        self.last_end: int | None = None
        self.cursors: set[str] = set()
        self.urls: set[str] = set()
        self.timestamps: set[int] = set()

    def accept(self, url: str, page: dict) -> str | None:
        if url != self.next_url:
            reject("SOURCE_CONFLICT", "pagination", "Page URL does not follow the original chain")
        if url in self.urls:
            reject("PAGINATION_LOOP", "next_url", "Repeated pagination URL")
        first, last, query = url_scope(url, self.request)
        cursor = query.get("cursor")
        if cursor is not None:
            if self.request.access.mode != "synthetic":
                reject(
                    "PAGINATION_UNVERIFIED",
                    "cursor",
                    "Licensed raw cursor inheritance is unverified",
                )
            if cursor in self.cursors:
                reject("PAGINATION_LOOP", "cursor", "Repeated opaque cursor")
            self.cursors.add(cursor)
        self.urls.add(url)
        rows = page.get("results", [])
        following = page.get("next_url")
        if not rows and (following is not None or page["queryCount"] != 0):
            reject("PAGINATION_INCOMPLETE", "results", "Empty page has no verifiable exhaustion")
        for row in rows:
            t = row.get("t")
            if type(t) is not int or not first <= t <= last:
                reject("DATA_INVALID", "t", "Bar timestamp is invalid or outside its page range")
            at = datetime.fromtimestamp(t / 1000, UTC)
            local = at.astimezone(NY)
            if t % 60000 or (self.request.frequency == "1d" and (local.hour or local.minute)):
                reject("DATA_INVALID", "t", "Bar does not align to its aggregate window")
            if t in self.timestamps:
                reject("DUPLICATE_BAR", "t", "Repeated timestamp in the source chain")
            if self.last_stamp is not None and t < self.last_stamp:
                reject("TIME_ORDER", "t", "Bars move backwards within the source chain")
            self.timestamps.add(t)
            self.last_stamp = t
            end = (
                at + timedelta(minutes=1)
                if self.request.frequency == "1m"
                else datetime.combine(
                    local.date() + timedelta(days=1), datetime.min.time(), NY
                ).astimezone(UTC)
            )
            self.last_end = int(end.timestamp() * 1000)
        if following is not None:
            next_first, _, next_query = url_scope(following, self.request)
            if following in self.urls or next_query.get("cursor") in self.cursors:
                reject("PAGINATION_LOOP", "next_url", "Repeated pagination URL/cursor")
            if next_first != first and next_first != self.last_end:
                reject(
                    "PAGE_INVALID",
                    "next_url",
                    "Continuation must retain start or advance to bar end",
                )
            if next_first == first and "cursor" not in next_query:
                reject("PAGINATION_LOOP", "next_url", "Noncursor continuation made no progress")
            if "cursor" in next_query and self.request.access.mode != "synthetic":
                reject(
                    "PAGINATION_UNVERIFIED",
                    "cursor",
                    "Licensed raw cursor inheritance is unverified",
                )
        self.next_url = following
        return following
