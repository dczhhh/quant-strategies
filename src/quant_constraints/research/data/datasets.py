"""Offline, bounded-day Parquet derivation and coverage reporting from frozen L0."""

import io
import json
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl

from quant_constraints.calendar import SessionCalendar

from .archive import ArchivedSnapshot, ArchiveInput, ArchiveStore, audit_archive
from .archive_contracts import ADAPTER_VERSION, canonical, digest, reject, wire
from .contracts import RawBar
from .massive import parse_page
from .normalize import normalize_identities, normalize_raw_bars

NY = ZoneInfo("America/New_York")
DATASET_VERSION = "research_dataset_v1"


def normalize_archived_snapshot(
    snapshot: ArchivedSnapshot, store: ArchiveStore
) -> ArchivedSnapshot:
    """Read no network; preserve original bytes and emit no trusted research bundle."""
    audit_archive(snapshot)
    manifest, request = snapshot.manifest, snapshot.manifest.request
    if manifest.layer != "source":
        reject("INVALID_REQUEST", "layer", "Normalization starts from the original source layer")
    if manifest.adapter_version != ADAPTER_VERSION:
        reject("UNSUPPORTED_MANIFEST", "adapter_version", "No decoder for this adapter version")
    identities = normalize_identities(json.loads(snapshot.read("identities.json")))
    buckets: dict[str, list[dict]] = defaultdict(list)
    seen: set[int] = set()
    excluded = outside = 0
    calendar = SessionCalendar()
    inputs = [
        ArchiveInput(item.path, item.role, snapshot.read(item.path), item.source_ref)
        for item in manifest.files
    ]
    parent_bytes = canonical(asdict(manifest))
    inputs.append(
        ArchiveInput(
            "source-manifest.json",
            "source_manifest",
            parent_bytes,
            "archive://" + manifest.revision,
        )
    )
    for file in manifest.files:
        if file.role != "response":
            continue
        page = parse_page(snapshot.read(file.path), request)
        receipt_path = file.path.removesuffix(".json") + ".receipt.json"
        receipt = json.loads(snapshot.read(receipt_path))
        ingested = datetime.fromisoformat(receipt["attempts"][-1]["ended_at"])
        for index, row in enumerate(page.get("results", [])):
            stamp = row.get("t")
            if type(stamp) is not int:
                reject("DATA_INVALID", "t", "Expected an integer Unix millisecond start")
            try:
                start = datetime.fromtimestamp(stamp / 1000, UTC)
            except (OverflowError, OSError, ValueError):
                reject("DATA_INVALID", "t", "Timestamp is outside supported bounds")
            local = start.astimezone(NY)
            if (
                start.second
                or start.microsecond
                or (request.frequency == "1d" and (local.hour or local.minute))
            ):
                reject("DATA_INVALID", "t", "Bar does not align to its declared aggregate window")
            end = (
                start + timedelta(minutes=1)
                if request.frequency == "1m"
                else datetime.combine(
                    local.date() + timedelta(days=1), datetime.min.time(), NY
                ).astimezone(UTC)
            )
            if not request.start_at <= start < request.end_at:
                outside += 1
                continue
            if end > request.end_at or end > ingested:
                reject(
                    "TIME_ORDER",
                    "bar_end_at",
                    "Incomplete aggregate or acquisition before bar completion",
                )
            if stamp in seen:
                reject("DUPLICATE_BAR", "t", "Repeated bar across source pages/partitions")
            seen.add(stamp)
            if len(seen) > 11000:
                reject("DATA_INVALID", "records", "Small-window record bound exceeded")
            source = {
                "provider": request.provider,
                "dataset_id": request.dataset_id,
                "dataset_revision": manifest.revision,
                "source_partition_id": request.request_id + ":" + file.path,
                "source_partition_ref": "archive://" + manifest.revision + "/" + file.path,
                "record_id": f"{index}:{stamp}",
                "record_version": 1,
                "ingested_at": ingested,
                "source_uri": file.source_ref,
                "source_timezone": "America/New_York",
            }
            record = {
                "security_id": request.security_id,
                "symbol": request.symbol,
                "venue": request.venue,
                "currency": request.currency,
                "product_id": "massive-raw-" + request.frequency,
                "bar_start_at": start,
                "bar_end_at": end,
                "available_at": ingested,
                "open": row.get("o"),
                "high": row.get("h"),
                "low": row.get("l"),
                "close": row.get("c"),
                "volume": row.get("v"),
                "price_basis": "raw",
                "share_unit": "as_traded",
                "volume_unit": "as_traded_shares",
                "source": source,
            }
            # Validate even filtered records; exclusions do not disguise bad prices/identities.
            bar = normalize_raw_bars((record,), identities)[0]
            session = calendar.session(local.date())
            if request.session_filter == "rth" and (
                session is None or start < session.market_open or end > session.market_close
            ):
                excluded += 1
                continue
            buckets[local.date().isoformat()].append(wire(asdict(bar)))
    partitions = []
    day = request.start_at.astimezone(NY).date()
    last_day = (request.end_at - timedelta(microseconds=1)).astimezone(NY).date()
    while day <= last_day:
        records = buckets.get(day.isoformat(), [])
        bars = normalize_raw_bars(records, identities)
        session = calendar.session(day)
        actual = {bar.bar_start_at for bar in bars}
        missing = []
        if session is not None:
            if request.frequency == "1m":
                cursor = max(session.market_open, request.start_at)
                finish = min(session.market_close, request.end_at)
                while cursor + timedelta(minutes=1) <= finish:
                    if cursor not in actual:
                        missing.append({"at": cursor.isoformat(), "reason": "unknown"})
                    cursor += timedelta(minutes=1)
            else:
                cursor = datetime.combine(day, datetime.min.time(), NY).astimezone(UTC)
                if request.start_at <= cursor < request.end_at and cursor not in actual:
                    missing.append({"at": cursor.isoformat(), "reason": "unknown"})
        info = {
            "trade_date": day.isoformat(),
            "bar_count": len(bars),
            "first_bar_at": bars[0].bar_start_at.isoformat() if bars else None,
            "last_bar_at": bars[-1].bar_start_at.isoformat() if bars else None,
            "session_open_at": session.market_open.isoformat() if session else None,
            "session_close_at": session.market_close.isoformat() if session else None,
            "missing_rth_windows": missing,
            "nontrading_day": session is None,
        }
        if bars:
            # One bounded security/day at a time; no forward-fill or adjusted-price conversion.
            encoded = [wire(asdict(bar)) for bar in bars]
            buffer = io.BytesIO()
            pl.DataFrame(encoded, infer_schema_length=None).write_parquet(
                buffer, compression="zstd", statistics=True
            )
            path = f"partitions/{request.security_id.encode().hex()}/{day.isoformat()}/{request.frequency}/raw.parquet"
            inputs.append(
                ArchiveInput(
                    path, "normalized_raw", buffer.getvalue(), "archive://" + manifest.revision
                )
            )
            info["file"] = path
        partitions.append(info)
        day += timedelta(days=1)
    report = {
        "schema_version": DATASET_VERSION,
        "status": "archive_ready",
        "market_data_verified": False,
        "request_id": request.request_id,
        "source_manifest_sha256": digest(parent_bytes),
        "request_start_at": request.start_at.isoformat(),
        "request_end_at": request.end_at.isoformat(),
        "coverage": wire(asdict(request.coverage)),
        "session_filter": request.session_filter,
        "timezone": "America/New_York",
        "bar_timestamp_convention": "start_inclusive_end_exclusive",
        "availability_method": "ingestion_upper_bound_unverified",
        "historical_correction_status": "not_compared_to_prior_acquisition",
        "raw_basis_evidence": "adjusted=false request and response declaration; pending 5C",
        "record_id_kind": "derived_page_row_timestamp_v1",
        "pages": sum(item.role == "response" for item in manifest.files),
        "excluded_by_session": excluded,
        "outside_request": outside,
        "partitions": partitions,
        "missing_evidence_roles": audit_archive(snapshot)["missing_evidence_roles"],
    }
    inputs.append(
        ArchiveInput(
            "archive-report.json", "report", canonical(report), "archive://" + manifest.revision
        )
    )
    return store.commit(
        request=request,
        layer="normalized",
        inputs=tuple(inputs),
        acquired_start_at=manifest.acquired_start_at,
        acquired_end_at=manifest.acquired_end_at,
        adapter_version=manifest.adapter_version,
        writer_version="polars-" + pl.__version__,
        source_manifest_sha256=digest(parent_bytes),
    )


def iter_raw_bars(dataset: ArchivedSnapshot) -> Iterator[RawBar]:
    """Replay v2 declarations from a verified *archive*, without certifying raw/PIT truth."""
    audit_archive(dataset)
    if dataset.manifest.layer != "normalized":
        reject("INVALID_REQUEST", "layer", "Replay requires normalized partitions")
    identities = normalize_identities(json.loads(dataset.read("identities.json")))
    for file in dataset.manifest.files:
        if file.role == "normalized_raw":
            records = pl.read_parquet(io.BytesIO(dataset.read(file.path))).to_dicts()
            yield from normalize_raw_bars(records, identities)
