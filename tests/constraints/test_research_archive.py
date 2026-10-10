"""Synthetic 5B acquisition/integrity/replay tests; no keys or market-data downloads."""

import json
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timedelta

import pytest
from test_research_data_contracts import identity

from quant_constraints.research.data.archive import (
    ArchiveInput,
    ArchiveStore,
    audit_archive,
    load_archive,
    safe_path,
    write_once,
)
from quant_constraints.research.data.archive_contracts import (
    AccessDeclaration,
    ArchiveError,
    ArchiveFile,
    ArchiveManifest,
    CoverageDeclaration,
    SourceRequest,
    canonical,
    digest,
)
from quant_constraints.research.data.contracts import ContractError
from quant_constraints.research.data.datasets import iter_raw_bars, normalize_archived_snapshot
from quant_constraints.research.data.massive import (
    HTTPResponse,
    MassiveAdapter,
    checked_url,
    request_url,
)
from quant_constraints.research.data.normalize import normalize_identities


def stamp(value="2024-06-07T13:30:00+00:00"):
    return datetime.fromisoformat(value)


def coverage(**changes):
    return CoverageDeclaration(
        **(
            {
                "market_scope": "us_consolidated",
                "source_feed": "synthetic-aggregate",
                "definition_ref": "fixture://provider-definition",
                "tapes": ("C", "A", "B"),
                "venues": ("XNAS", "XNYS"),
                "included": (),
                "excluded": (),
                "unknown": ("trf", "auction", "odd_lot", "out_of_session"),
                "trade_condition_policy": "fixture://eligibility",
                "correction_policy": "historical_final_revision_unverified",
                "quote_availability": "not_collected",
            }
            | changes
        )
    )


def access(**changes):
    return AccessDeclaration(
        **(
            {
                "mode": "synthetic",
                "plan": "offline-fixture",
                "license_ref": "fixture://publishable-synthetic-only",
                "archive_allowed": True,
                "history_start_at": stamp("2020-01-01T00:00:00+00:00"),
                "history_end_at": stamp("2030-01-01T00:00:00+00:00"),
                "frequencies": ("1m", "1d"),
            }
            | changes
        )
    )


def source_request(**changes):
    return SourceRequest(
        **(
            {
                "dataset_id": "synthetic-archive",
                "security_id": "fixture-security-A",
                "symbol": "AAA",
                "venue": "XNAS",
                "currency": "USD",
                "start_at": stamp(),
                "end_at": stamp() + timedelta(minutes=3),
                "frequency": "1m",
                "session_filter": "rth",
                "coverage": coverage(),
                "access": access(),
            }
            | changes
        )
    )


def row(at=None, **changes):
    return {
        "t": int((at or stamp()).timestamp() * 1000),
        "o": 100,
        "h": 101,
        "l": 99,
        "c": 100.5,
        "v": 100.25,
    } | changes


def page(rows=None, **changes):
    records = [row()] if rows is None else rows
    return canonical(
        {
            "status": "OK",
            "ticker": "AAA",
            "adjusted": False,
            "queryCount": len(records),
            "resultsCount": len(records),
            "request_id": "synthetic-provider-id",
            "results": records,
        }
        | changes
    )


class Clock:
    def __init__(self):
        self.at = stamp("2026-10-10T11:00:00+00:00")

    def __call__(self):
        result = self.at
        self.at += timedelta(seconds=1)
        return result


class Transport:
    def __init__(self, items):
        self.items = iter(items)
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers, timeout))
        value = next(self.items)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, HTTPResponse) else HTTPResponse(200, value)


def adapter(transport, **changes):
    return MassiveAdapter(
        **(
            {
                "transport": transport,
                "clock": Clock(),
                "sleeper": lambda _: None,
                "minimum_interval": 0,
                "retries": 0,
            }
            | changes
        )
    )


def archive(tmp_path, *, request=None, payload=None, evidence=()):
    request = request or source_request()
    transport = Transport([page() if payload is None else payload])
    store = ArchiveStore(tmp_path / "archives")
    snapshot = adapter(transport).acquire_and_archive(
        request, store, normalize_identities([identity()]), evidence=evidence
    )
    return store, snapshot


def test_request_roundtrip_determinism_and_deep_immutability():
    request = source_request()
    restored = SourceRequest.from_dict(json.loads(canonical(asdict(request))))
    assert restored == request and restored.request_id == request.request_id
    assert source_request(coverage=coverage(tapes=("A", "B", "C"))).request_id == request.request_id
    assert request.coverage.tapes == ("A", "B", "C")
    with pytest.raises(FrozenInstanceError):
        request.symbol = "BBB"
    with pytest.raises(FrozenInstanceError):
        request.coverage.market_scope = "single_venue"
    assert "apiKey" not in canonical(asdict(request)).decode()


@pytest.mark.parametrize(
    "change,code",
    [
        ({"adjusted": True}, "INVALID_REQUEST"),
        ({"adjusted": 0}, "INVALID_REQUEST"),
        ({"frequency": "5m"}, "INVALID_REQUEST"),
        ({"currency": "CNH"}, "INVALID_REQUEST"),
        ({"symbol": "AAA/apiKey=x"}, "INVALID_REQUEST"),
        ({"provider": "yahoo"}, "INVALID_REQUEST"),
        ({"end_at": stamp()}, "INVALID_REQUEST"),
        ({"end_at": stamp() + timedelta(days=8)}, "INVALID_REQUEST"),
        ({"page_limit": True}, "INVALID_REQUEST"),
        ({"page_limit": 50001}, "INVALID_REQUEST"),
        ({"schema_version": "research_data_v1"}, "UNSUPPORTED_MANIFEST"),
        ({"normalizer_version": "other"}, "UNSUPPORTED_MANIFEST"),
        ({"session_filter": "guess"}, "INVALID_REQUEST"),
        ({"end_at": stamp() + timedelta(microseconds=1)}, "INVALID_REQUEST"),
    ],
)
def test_request_rejections(change, code):
    with pytest.raises(ArchiveError) as error:
        source_request(**change)
    assert error.value.code == code
    assert error.value.as_dict()["schema_version"] == "research_archive_v1"


@pytest.mark.parametrize(
    "change", [{"archive_allowed": False}, {"archive_allowed": 1}, {"mode": "public_download"}]
)
def test_archival_permission_is_explicit(change):
    with pytest.raises(ArchiveError, match="UNAUTHORIZED"):
        access(**change)


def test_request_outside_declared_permissions_is_unavailable():
    with pytest.raises(ArchiveError, match="UNAUTHORIZED"):
        source_request(access=access(history_start_at=stamp() + timedelta(minutes=1)))
    with pytest.raises(ArchiveError, match="UNAUTHORIZED"):
        source_request(access=access(frequencies=("1d",)))


@pytest.mark.parametrize(
    "change",
    [
        {"included": ("trf",)},
        {"unknown": ()},
        {"included": ("invented",)},
        {"market_scope": "unknown"},
        {"tapes": ("D",)},
        {"quote_availability": "nbbo_verified"},
        {"venues": ["XNAS"]},
        {"unknown": ("trf", "trf")},
    ],
)
def test_coverage_declarations_reject_ambiguity(change):
    with pytest.raises(ArchiveError):
        coverage(**change)


def test_scope_and_exclusions_are_preserved_without_false_full_market_claim(tmp_path):
    single = source_request(coverage=coverage(market_scope="single_venue", venues=("XNAS",)))
    assert single.request_id != source_request().request_id
    with pytest.raises(ArchiveError, match="UNSUPPORTED_SCOPE"):
        adapter(Transport([])).acquire_and_archive(
            single, ArchiveStore(tmp_path), normalize_identities([identity()])
        )
    declared = coverage(
        included=("trf",), excluded=("odd_lot",), unknown=("auction", "out_of_session")
    )
    store, snapshot = archive(tmp_path, request=source_request(coverage=declared))
    dataset = normalize_archived_snapshot(snapshot, store)
    report = json.loads(dataset.read("archive-report.json"))
    assert report["coverage"]["included"] == ["trf"]
    assert report["coverage"]["excluded"] == ["odd_lot"]
    assert report["market_data_verified"] is False


def test_native_download_requires_credential_and_private_permission(tmp_path, monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    for request in (source_request(), source_request(access=access(mode="licensed_private"))):
        with pytest.raises(ArchiveError, match="UNAUTHORIZED"):
            MassiveAdapter().acquire_and_archive(
                request, ArchiveStore(tmp_path), normalize_identities([identity()])
            )


def test_pagination_retry_resume_idempotence_and_offline_replay(tmp_path):
    request, identities = source_request(), normalize_identities([identity()])
    cursor = request_url(request).split("?")[0] + "?cursor=page-2"
    first = page([row(stamp() + timedelta(minutes=2))], next_url=cursor)
    second = page([row(stamp() + timedelta(minutes=1)), row()])
    transport = Transport([HTTPResponse(429, b"{}"), first, TimeoutError("synthetic timeout")])
    waits = []
    client = adapter(transport, retries=1, sleeper=waits.append)
    store = ArchiveStore(tmp_path)
    # Exhaustion must leave a fully committed first page but no completed snapshot.
    transport.items = iter([HTTPResponse(429, b"{}"), first, TimeoutError(), TimeoutError()])
    with pytest.raises(ArchiveError, match="RETRY_EXHAUSTED"):
        client.acquire_and_archive(request, store, identities)
    assert len(transport.calls) == 4 and 0.5 in waits
    transport.items = iter([second])
    source = client.acquire_and_archive(request, store, identities)
    assert len(transport.calls) == 5  # first successful page was not refetched
    assert source.read("pages/0000.json") == first
    assert source.read("pages/0001.json") == second
    cached = client.acquire_and_archive(request, store, identities)
    assert cached == source and len(transport.calls) == 5
    dataset = normalize_archived_snapshot(source, store)
    rebuilt = normalize_archived_snapshot(load_archive(source.directory), store)
    assert rebuilt.manifest == dataset.manifest
    bars = tuple(iter_raw_bars(load_archive(dataset.directory)))
    assert [bar.bar_start_at for bar in bars] == [stamp() + timedelta(minutes=i) for i in range(3)]
    assert len({bar.source.source_partition_id for bar in bars}) == 2
    assert bars[0].source.record_id.startswith("1:")
    assert all(
        bar.available_at == bar.source.ingested_at and bar.available_at.year == 2026 for bar in bars
    )
    assert (
        json.loads(dataset.read("archive-report.json"))["partitions"][0]["missing_rth_windows"]
        == []
    )


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "PERMISSION_GAP"),
        (403, "PERMISSION_GAP"),
        (302, "TRANSPORT_REJECTED"),
        (404, "TRANSPORT_REJECTED"),
        (500, "RETRY_EXHAUSTED"),
    ],
)
def test_bad_http_does_not_create_empty_dataset(tmp_path, status, code):
    with pytest.raises(ArchiveError) as error:
        archive(tmp_path, payload=HTTPResponse(status, b"{}"))
    assert error.value.code == code
    assert not list((tmp_path / "archives").glob("source/*"))


@pytest.mark.parametrize(
    "change,code",
    [
        ({"adjusted": True}, "PAGE_INVALID"),
        ({"ticker": "BBB"}, "PAGE_INVALID"),
        ({"status": "NOT_AUTHORIZED"}, "PAGE_INVALID"),
        ({"resultsCount": 9}, "PAGE_INVALID"),
        ({"queryCount": True}, "PAGE_INVALID"),
        ({"request_id": None}, "PAGE_INVALID"),
        ({"results": "not-rows"}, "PAGE_INVALID"),
        ({"next_url": "https://evil.invalid/page"}, "PAGE_INVALID"),
        ({"queryCount": 50000}, "PAGINATION_INCOMPLETE"),
    ],
)
def test_invalid_page_rejected_before_snapshot(tmp_path, change, code):
    with pytest.raises(ArchiveError) as error:
        archive(tmp_path, payload=page(**change))
    assert error.value.code == code


def test_page_loop_and_page_bound(tmp_path):
    request = source_request()
    ids = normalize_identities([identity()])
    store = ArchiveStore(tmp_path)
    for acquisition_id, max_pages, cursor, code in [
        ("loop", 3, request_url(request), "PAGINATION_LOOP"),
        ("limit", 1, request_url(request).split("?")[0] + "?cursor=2", "PAGINATION_INCOMPLETE"),
    ]:
        with pytest.raises(ArchiveError, match=code):
            adapter(Transport([page(next_url=cursor)]), max_pages=max_pages).acquire_and_archive(
                request, store, ids, acquisition_id=acquisition_id
            )


def test_credentials_never_reach_archive_or_error_messages(tmp_path):
    token = "synthetic-secret-1234"
    transport = Transport(
        [HTTPResponse(200, page(), (("Authorization", token), ("X-Request-ID", "safe")))]
    )
    client = adapter(transport, api_key=token)
    source = client.acquire_and_archive(
        source_request(), ArchiveStore(tmp_path), normalize_identities([identity()])
    )
    assert transport.calls[0][1]["Authorization"] == "Bearer " + token
    assert token not in repr(client)
    assert all(
        token.encode() not in file.read_bytes() for file in tmp_path.rglob("*") if file.is_file()
    )
    for index, body in enumerate(
        [page(extra=token), page(next_url=request_url(source_request()) + "&apiKey=" + token)]
    ):
        with pytest.raises(ArchiveError) as error:
            adapter(Transport([body]), api_key=token).acquire_and_archive(
                source_request(),
                ArchiveStore(tmp_path),
                normalize_identities([identity()]),
                acquisition_id=f"secret-{index}",
            )
        assert error.value.code == "KEY_EXPOSURE" and token not in str(error.value)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"t": True}, "DATA_INVALID"),
        ({"t": 1.2}, "DATA_INVALID"),
        ({"t": int(stamp().timestamp() * 1000) + 1}, "DATA_INVALID"),
        ({"t": 10**30}, "DATA_INVALID"),
        ({"h": 90}, "INVALID_OHLCV"),
        ({"v": -1}, "INVALID_VALUE"),
    ],
)
def test_bad_bars_reject_normalization_without_losing_originals(tmp_path, change, code):
    store, snapshot = archive(tmp_path, payload=page([row(**change)]))
    with pytest.raises((ArchiveError, ContractError)) as error:
        normalize_archived_snapshot(snapshot, store)
    assert str(error.value.code) == code
    assert audit_archive(snapshot)["market_data_verified"] is False
    assert not list(store.root.glob("normalized/*"))


def test_duplicate_bars_across_pages_are_not_legitimized_by_new_partition(tmp_path):
    request = source_request()
    cursor = request_url(request).split("?")[0] + "?cursor=2"
    client = adapter(Transport([page(next_url=cursor), page()]))
    store = ArchiveStore(tmp_path)
    source = client.acquire_and_archive(request, store, normalize_identities([identity()]))
    with pytest.raises(ArchiveError, match="DUPLICATE_BAR"):
        normalize_archived_snapshot(source, store)


@pytest.mark.parametrize(
    "path",
    [
        "pages/0000.json",
        "request.json",
        "identities.json",
        "actions.json",
        "factors.json",
        "comparison.json",
    ],
)
def test_any_evidence_change_invalidates_the_archive(tmp_path, path):
    evidence = tuple(
        ArchiveInput(name + ".json", role, b"[]", "fixture://" + name)
        for name, role in [
            ("actions", "corporate_actions"),
            ("factors", "adjustment_factors"),
            ("comparison", "comparison"),
        ]
    )
    store, source = archive(tmp_path, evidence=evidence)
    dataset = normalize_archived_snapshot(source, store)
    (source.directory / path).write_bytes(b"changed")
    with pytest.raises(ArchiveError, match="HASH_MISMATCH"):
        normalize_archived_snapshot(source, store)
    # The derived snapshot preserves its own independent copies and all hashes.
    assert len(tuple(iter_raw_bars(dataset))) == 1


def test_normalized_file_and_manifest_tampering_rejects_before_replay(tmp_path):
    store, source = archive(tmp_path)
    dataset = normalize_archived_snapshot(source, store)
    partition = next(item for item in dataset.manifest.files if item.role == "normalized_raw")
    original = (dataset.directory / partition.path).read_bytes()
    (dataset.directory / partition.path).write_bytes(original + b"x")
    with pytest.raises(ArchiveError, match="HASH_MISMATCH"):
        next(iter_raw_bars(dataset))
    (dataset.directory / partition.path).write_bytes(original)
    encoded = json.loads((dataset.directory / "manifest.json").read_bytes())
    encoded["request"]["venue"] = "XNYS"
    changed = canonical(encoded)
    (dataset.directory / "manifest.json").write_bytes(changed)
    (dataset.directory / "manifest.sha256").write_text(digest(changed))
    with pytest.raises(ArchiveError, match="REVISION_CONFLICT"):
        load_archive(dataset.directory)


def test_new_historical_revision_preserves_both_original_snapshots(tmp_path):
    store = ArchiveStore(tmp_path)
    client = adapter(Transport([page(), page([row(c=100.7)])]))
    ids, request = normalize_identities([identity()]), source_request()
    old = client.acquire_and_archive(request, store, ids)
    new = client.acquire_and_archive(request, store, ids, acquisition_id="refetch-2")
    assert old.manifest.revision != new.manifest.revision
    assert old.read("pages/0000.json") != new.read("pages/0000.json")
    old_data, new_data = (normalize_archived_snapshot(item, store) for item in (old, new))
    assert next(iter_raw_bars(old_data)).close == 100.5
    assert next(iter_raw_bars(new_data)).close == 100.7
    assert (
        json.loads(new_data.read("archive-report.json"))["historical_correction_status"]
        == "not_compared_to_prior_acquisition"
    )


def test_missing_bar_is_unknown_and_never_fabricated_or_zero_filled(tmp_path):
    store, source = archive(tmp_path, payload=page([row(), row(stamp() + timedelta(minutes=2))]))
    data = normalize_archived_snapshot(source, store)
    report = json.loads(data.read("archive-report.json"))
    assert report["partitions"][0]["missing_rth_windows"] == [
        {"at": (stamp() + timedelta(minutes=1)).isoformat(), "reason": "unknown"}
    ]
    assert len(tuple(iter_raw_bars(data))) == 2
    assert report["status"] == "archive_ready" and report["market_data_verified"] is False


@pytest.mark.parametrize(
    "date_start,date_end,count",
    [
        ("2024-06-07T13:30:00+00:00", "2024-06-07T20:00:00+00:00", 390),
        ("2024-07-03T13:30:00+00:00", "2024-07-03T17:00:00+00:00", 210),
    ],
)
def test_full_and_early_close_sessions(tmp_path, date_start, date_end, count):
    start, end = stamp(date_start), stamp(date_end)
    request = source_request(start_at=start, end_at=end)
    store, source = archive(
        tmp_path,
        request=request,
        payload=page([row(start + timedelta(minutes=i)) for i in reversed(range(count))]),
    )
    data = normalize_archived_snapshot(source, store)
    assert len(tuple(iter_raw_bars(data))) == count
    info = json.loads(data.read("archive-report.json"))["partitions"][0]
    assert info["missing_rth_windows"] == [] and stamp(info["session_close_at"]) == end


def test_dst_and_nontrading_empty_response(tmp_path):
    start, end = stamp("2024-03-08T14:30:00+00:00"), stamp("2024-03-11T13:31:00+00:00")
    store, source = archive(
        tmp_path / "dst",
        request=source_request(start_at=start, end_at=end),
        payload=page([row(start), row(end - timedelta(minutes=1))]),
    )
    data = normalize_archived_snapshot(source, store)
    info = json.loads(data.read("archive-report.json"))["partitions"]
    assert (
        stamp(info[0]["session_open_at"]).hour == 14
        and stamp(info[-1]["session_open_at"]).hour == 13
    )
    assert info[1]["nontrading_day"] and info[2]["nontrading_day"]
    saturday = stamp("2024-06-08T13:30:00+00:00")
    store, source = archive(
        tmp_path / "empty",
        request=source_request(start_at=saturday, end_at=saturday + timedelta(minutes=3)),
        payload=page([]),
    )
    data = normalize_archived_snapshot(source, store)
    assert tuple(iter_raw_bars(data)) == ()
    assert json.loads(data.read("archive-report.json"))["partitions"][0]["nontrading_day"]


def test_rth_filter_and_daily_comparison_clock(tmp_path):
    start = stamp("2024-06-07T13:29:00+00:00")
    store, source = archive(
        tmp_path / "minute",
        request=source_request(start_at=start),
        payload=page([row(start), row()]),
    )
    data = normalize_archived_snapshot(source, store)
    assert len(tuple(iter_raw_bars(data))) == 1
    assert json.loads(data.read("archive-report.json"))["excluded_by_session"] == 1
    midnight = stamp("2024-06-07T04:00:00+00:00")
    request = source_request(
        start_at=midnight,
        end_at=midnight + timedelta(days=1),
        frequency="1d",
        session_filter="all_source_sessions",
    )
    store, source = archive(tmp_path / "day", request=request, payload=page([row(midnight)]))
    bar = next(iter_raw_bars(normalize_archived_snapshot(source, store)))
    assert bar.bar_end_at - bar.bar_start_at == timedelta(days=1)
    with pytest.raises(ArchiveError):
        replace(request, session_filter="rth")


def test_identity_boundary_and_rename_requests_are_explicit(tmp_path):
    boundary = stamp() + timedelta(minutes=2)
    ids = normalize_identities(
        [
            identity(effective_until=boundary),
            identity(
                symbol="BBB",
                effective_from=boundary,
                transition="rename",
                source=identity()["source"] | {"record_id": "rename"},
            ),
        ]
    )
    request = source_request()
    with pytest.raises(ArchiveError, match="IDENTITY_CONFLICT"):
        adapter(Transport([])).acquire_and_archive(request, ArchiveStore(tmp_path), ids)
    store = ArchiveStore(tmp_path)
    client = adapter(Transport([page([row(boundary)], ticker="BBB")]))
    source = client.acquire_and_archive(
        replace(request, symbol="BBB", start_at=boundary), store, ids
    )
    assert next(iter_raw_bars(normalize_archived_snapshot(source, store))).symbol == "BBB"


@pytest.mark.parametrize(
    "path", ["../escape", "/absolute", "a/../b", "a\\b", "./a", "manifest.json"]
)
def test_manifest_paths_never_escape_or_alias_reserved_files(path):
    with pytest.raises(ArchiveError, match="PATH_INVALID"):
        ArchiveFile(
            path=path,
            role="response",
            sha256=digest(b"x"),
            size_bytes=1,
            source_ref="fixture://source",
        )


def test_write_once_rejects_replacement_and_inventory_mismatch(tmp_path):
    path = tmp_path / "exclusive"
    write_once(path, b"one")
    write_once(path, b"one")
    with pytest.raises(ArchiveError, match="REVISION_CONFLICT"):
        write_once(path, b"two")
    store, source = archive(tmp_path / "source")
    (source.directory / "unexpected").write_bytes(b"new")
    with pytest.raises(ArchiveError, match="HASH_MISMATCH"):
        audit_archive(source)
    with pytest.raises(ArchiveError, match="PATH_INVALID"):
        safe_path(tmp_path, "../outside")


def test_manifest_roundtrip_and_strict_version(tmp_path):
    _, source = archive(tmp_path)
    record = json.loads(canonical(asdict(source.manifest)))
    assert ArchiveManifest.from_dict(record) == source.manifest
    for changes in ({"schema_version": "research_archive_v2"}, {"verified": True}, {"files": []}):
        with pytest.raises(ArchiveError):
            ArchiveManifest.from_dict(record | changes)


@pytest.mark.parametrize(
    "change",
    [
        {"retries": -1},
        {"retries": True},
        {"max_pages": 0},
        {"max_pages": 101},
        {"timeout": 0},
        {"timeout": float("nan")},
        {"minimum_interval": 61},
        {"api_key": "bad key"},
    ],
)
def test_resource_bounds_cannot_be_disabled(change):
    with pytest.raises(ArchiveError, match="INVALID_REQUEST"):
        MassiveAdapter(**change)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.invalid/x",
        "http://api.massive.com/x",
        "https://api.massive.com.evil.invalid/x",
        "https://api.massive.com/v2/aggs/ticker/BBB/range/1/minute/x",
        "https://[broken",
        "https://api.massive.com/v2/aggs/ticker/AAA/range/1/minute/x?adjusted=true",
    ],
)
def test_pagination_never_changes_origin_ticker_or_adjustment(url):
    with pytest.raises(ArchiveError, match="PAGE_INVALID"):
        checked_url(url, source_request())


def test_retry_after_and_5xx_bodies_are_retained(tmp_path):
    waits = []
    transport = Transport(
        [HTTPResponse(503, b"synthetic upstream unavailable", (("Retry-After", "2"),)), page()]
    )
    client = adapter(transport, retries=1, sleeper=waits.append)
    source = client.acquire_and_archive(
        source_request(), ArchiveStore(tmp_path), normalize_identities([identity()])
    )
    assert 2 in waits
    assert source.read("pages/0000.attempt-00.bin") == b"synthetic upstream unavailable"
    receipt = json.loads(source.read("pages/0000.receipt.json"))
    assert [item["status"] for item in receipt["attempts"]] == [503, 200]
    assert audit_archive(source)["status"] == "archive_complete"


def test_pending_checkpoint_corruption_cannot_be_resumed(tmp_path):
    request, ids = source_request(), normalize_identities([identity()])
    cursor = request_url(request).split("?")[0] + "?cursor=2"
    client = adapter(Transport([page(next_url=cursor), TimeoutError()]))
    store = ArchiveStore(tmp_path)
    with pytest.raises(ArchiveError, match="RETRY_EXHAUSTED"):
        client.acquire_and_archive(request, store, ids)
    pending = store.root / "sessions" / request.request_id / "initial"
    (pending / "pages/0000.json").write_bytes(b"changed")
    with pytest.raises(ArchiveError, match="HASH_MISMATCH"):
        client.acquire_and_archive(request, store, ids)


def test_completed_checkpoint_and_manifest_digest_are_strict(tmp_path):
    store, source = archive(tmp_path)
    client = adapter(Transport([]))
    checkpoint = (
        store.root / "sessions" / source.manifest.request.request_id / "initial" / "completed.json"
    )
    checkpoint.write_text('{"revision":"../outside"}')
    with pytest.raises(ArchiveError, match="HASH_MISMATCH"):
        client.acquire_and_archive(
            source.manifest.request, store, normalize_identities([identity()])
        )
    (source.directory / "manifest.sha256").write_text("wrong")
    with pytest.raises(ArchiveError, match="HASH_MISMATCH"):
        load_archive(source.directory)


def test_missing_rth_day_reports_unknown_not_permission_or_zero_volume(tmp_path):
    start, end = stamp(), stamp() + timedelta(minutes=2)
    store, source = archive(
        tmp_path, request=source_request(start_at=start, end_at=end), payload=page([])
    )
    data = normalize_archived_snapshot(source, store)
    info = json.loads(data.read("archive-report.json"))["partitions"][0]
    assert len(info["missing_rth_windows"]) == 2
    assert {gap["reason"] for gap in info["missing_rth_windows"]} == {"unknown"}
    assert tuple(iter_raw_bars(data)) == ()


def test_native_transport_permission_and_environment_key_with_mock_http(tmp_path, monkeypatch):
    import quant_constraints.research.data.massive as massive

    token = "synthetic-env-token"
    transport = Transport([page()])
    monkeypatch.setenv("MASSIVE_API_KEY", token)
    monkeypatch.setattr(massive, "http_get", transport)
    client = MassiveAdapter(clock=Clock(), sleeper=lambda _: None, minimum_interval=0)
    source = client.acquire_and_archive(
        source_request(access=access(mode="licensed_private")),
        ArchiveStore(tmp_path),
        normalize_identities([identity()]),
    )
    assert transport.calls[0][1]["Authorization"] == "Bearer " + token
    assert token.encode() not in source.read("request.json")


def test_http_transport_disables_redirects_and_preserves_http_errors(monkeypatch):
    import io
    from urllib.error import HTTPError

    import quant_constraints.research.data.massive as massive

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, bound):
            return b"original bytes"

    class Opener:
        def open(self, req, timeout):
            return Response()

    handlers = []
    monkeypatch.setattr(
        massive, "build_opener", lambda handler: handlers.append(handler) or Opener()
    )
    response = massive.http_get(request_url(source_request()), {}, 5)
    assert response.body == b"original bytes"
    assert (
        handlers[0].redirect_request(None, None, 302, "redirect", {}, "https://evil.invalid")
        is None
    )

    error_body = io.BytesIO(b"limited")

    class ErrorOpener:
        def open(self, req, timeout):
            raise HTTPError(req.full_url, 429, "rate limited", {"Retry-After": "3"}, error_body)

    monkeypatch.setattr(massive, "build_opener", lambda _: ErrorOpener())
    error_response = massive.http_get(request_url(source_request()), {}, 5)
    assert error_response == HTTPResponse(429, b"limited", (("Retry-After", "3"),))
    assert error_body.closed
    with pytest.raises(ArchiveError):
        massive.http_get("file:///etc/passwd", {}, 5)


def test_http_error_body_is_closed_when_read_fails(monkeypatch):
    import io
    from urllib.error import HTTPError

    import quant_constraints.research.data.massive as massive

    class UnreadableBody(io.BytesIO):
        def read(self, bound):
            raise OSError("interrupted response")

    body = UnreadableBody(b"partial")

    class ErrorOpener:
        def open(self, req, timeout):
            raise HTTPError(req.full_url, 503, "unavailable", {}, body)

    monkeypatch.setattr(massive, "build_opener", lambda _: ErrorOpener())
    with pytest.raises(OSError, match="interrupted response"):
        massive.http_get(request_url(source_request()), {}, 5)
    assert body.closed


def test_malformed_json_and_response_byte_budget(tmp_path, monkeypatch):
    import quant_constraints.research.data.massive as massive

    for index, body in enumerate((b"not JSON", b'{"status":NaN}')):
        with pytest.raises(ArchiveError, match="PAGE_INVALID"):
            archive(tmp_path / str(index), payload=body)
    monkeypatch.setattr(massive, "MAX_RESPONSE_BYTES", 1)
    with pytest.raises(ArchiveError, match="PAGE_INVALID"):
        archive(tmp_path / "bound")


def test_archive_request_and_receipt_cannot_claim_different_sources(tmp_path):
    store, source = archive(tmp_path)
    inputs = [
        ArchiveInput(item.path, item.role, source.read(item.path), item.source_ref)
        for item in source.manifest.files
    ]
    wrong_request = replace(source.manifest.request, venue="XNYS")
    with pytest.raises(ArchiveError, match="SOURCE_CONFLICT"):
        store.commit(
            request=wrong_request,
            layer="source",
            inputs=tuple(inputs),
            acquired_start_at=source.manifest.acquired_start_at,
            acquired_end_at=source.manifest.acquired_end_at,
            adapter_version=source.manifest.adapter_version,
            writer_version="original_bytes_v1",
        )
    assert len(list(store.root.glob("source/*"))) == 1


def test_outside_rows_are_disclosed_and_incomplete_bars_reject(tmp_path):
    request = source_request()
    store, source = archive(
        tmp_path / "outside", payload=page([row(stamp() - timedelta(minutes=1)), row()])
    )
    data = normalize_archived_snapshot(source, store)
    assert json.loads(data.read("archive-report.json"))["outside_request"] == 1
    assert len(tuple(iter_raw_bars(data))) == 1
    with pytest.raises(ArchiveError):
        source_request(frequency="1d", session_filter="all_source_sessions")
    client = adapter(Transport([page()]), clock=lambda: request.start_at)
    with pytest.raises(ArchiveError, match="TIME_ORDER"):
        client.acquire_and_archive(
            request, ArchiveStore(tmp_path / "future"), normalize_identities([identity()])
        )


def test_evidence_reference_credentials_are_rejected_before_persistence(tmp_path):
    token = "synthetic-reference-secret"
    evidence = (
        ArchiveInput(
            "policy.txt", "source_definition", b"policy", "https://provider.invalid/" + token
        ),
    )
    with pytest.raises(ArchiveError, match="KEY_EXPOSURE"):
        adapter(Transport([]), api_key=token).acquire_and_archive(
            source_request(),
            ArchiveStore(tmp_path),
            normalize_identities([identity()]),
            evidence=evidence,
        )
    assert all(
        token.encode() not in file.read_bytes() for file in tmp_path.rglob("*") if file.is_file()
    )


def test_all_evidence_roles_and_public_opt_in_replay(tmp_path):
    from quant_constraints.research.data import ingestion

    evidence = tuple(
        ArchiveInput(name + ".txt", name, b"synthetic evidence", "fixture://" + name)
        for name in (
            "corporate_actions",
            "adjustment_factors",
            "comparison",
            "source_definition",
            "license_terms",
        )
    )
    store, source = archive(tmp_path, evidence=evidence)
    data = ingestion.normalize_archived_snapshot(source, store)
    assert ingestion.audit_archive(data)["missing_evidence_roles"] == []
    assert len(tuple(ingestion.iter_raw_bars(ingestion.load_archive(data.directory)))) == 1


def test_total_bytes_and_checkpoint_identity_mutation_are_rejected(tmp_path, monkeypatch):
    import quant_constraints.research.data.massive as massive

    monkeypatch.setattr(massive, "MAX_ACQUISITION_BYTES", 1)
    with pytest.raises(ArchiveError, match="PAGE_INVALID"):
        archive(tmp_path / "bytes")
    monkeypatch.setattr(massive, "MAX_ACQUISITION_BYTES", 64 * 1024 * 1024)
    store, source = archive(tmp_path / "identity")
    changed = normalize_identities(
        [identity(source=identity()["source"] | {"record_id": "revised-identity"})]
    )
    with pytest.raises(ArchiveError, match="REVISION_CONFLICT"):
        adapter(Transport([])).acquire_and_archive(source.manifest.request, store, changed)


@pytest.mark.parametrize("path", ["C:/drive", "a\x00b"])
def test_paths_are_portable_and_nul_free(path):
    with pytest.raises(ArchiveError, match="PATH_INVALID"):
        ArchiveInput(path, "response", b"x", "fixture://source")


def test_corrupt_bound_reads_and_missing_files_are_rejected(tmp_path):
    _, source = archive(tmp_path)
    with pytest.raises(ArchiveError, match="PATH_INVALID"):
        source.read("not-in-manifest")
    (source.directory / "pages/0000.json").unlink()
    with pytest.raises(ArchiveError, match="HASH_MISMATCH"):
        source.read("pages/0000.json")
    with pytest.raises(ArchiveError, match="HASH_MISMATCH"):
        audit_archive(source)


@pytest.mark.parametrize("changes", [{"size_bytes": True}, {"sha256": "bad"}, {"role": "unbound"}])
def test_file_descriptors_are_strict(changes):
    with pytest.raises(ArchiveError):
        ArchiveFile(
            **(
                {
                    "path": "data.bin",
                    "role": "response",
                    "sha256": digest(b"x"),
                    "size_bytes": 1,
                    "source_ref": "fixture://source",
                }
                | changes
            )
        )


def test_archive_layers_cannot_be_used_interchangeably(tmp_path):
    store, source = archive(tmp_path)
    with pytest.raises(ArchiveError, match="INVALID_REQUEST"):
        next(iter_raw_bars(source))
    data = normalize_archived_snapshot(source, store)
    with pytest.raises(ArchiveError, match="INVALID_REQUEST"):
        normalize_archived_snapshot(data, store)


@pytest.mark.parametrize("replacement", ["revision_only", "whole_checkpoint"])
@pytest.mark.parametrize(
    "difference", ["dataset", "time", "security", "identity", "evidence", "acquisition", "page"]
)
def test_completed_checkpoint_cannot_reference_another_valid_archive(
    tmp_path, replacement, difference
):
    import shutil

    request = source_request()
    ids = normalize_identities([identity()])
    evidence = (
        ArchiveInput("evidence/action.json", "corporate_actions", b"v1", "fixture://action"),
    )
    store = ArchiveStore(tmp_path / "A")
    original = adapter(Transport([page()])).acquire_and_archive(
        request, store, ids, evidence=evidence
    )
    other_request, other_ids, other_evidence, other_id = request, ids, evidence, "initial"
    if difference == "dataset":
        other_request = replace(request, dataset_id="another-dataset")
    elif difference == "time":
        other_request = replace(request, start_at=request.start_at + timedelta(minutes=1))
    elif difference == "security":
        other_request = replace(request, symbol="BBB", security_id="fixture-security-B")
        other_ids = normalize_identities([identity(symbol="BBB", security_id="fixture-security-B")])
    elif difference == "identity":
        other_ids = normalize_identities(
            [identity(source=identity()["source"] | {"record_id": "v2"})]
        )
    elif difference == "evidence":
        other_evidence = (replace(evidence[0], content=b"v2"),)
    elif difference == "acquisition":
        other_id = "refetch"
    payload = page(
        [row(other_request.start_at, c=100.75 if difference == "page" else 100.5)],
        ticker=other_request.symbol,
    )
    other_store = ArchiveStore(tmp_path / "B")
    other = adapter(Transport([payload])).acquire_and_archive(
        other_request, other_store, other_ids, acquisition_id=other_id, evidence=other_evidence
    )
    assert other.manifest.revision != original.manifest.revision
    target = store.root / "source" / other.manifest.revision
    shutil.copytree(other.directory, target)
    assert load_archive(target).manifest == other.manifest
    checkpoint = store.root / "sessions" / request.request_id / "initial" / "completed.json"
    other_checkpoint = (
        other_store.root / "sessions" / other_request.request_id / other_id / "completed.json"
    )
    if replacement == "revision_only":
        changed = json.loads(checkpoint.read_bytes()) | {"revision": other.manifest.revision}
        checkpoint.write_bytes(canonical(changed))
    else:
        checkpoint.write_bytes(other_checkpoint.read_bytes())
    transport = Transport([])
    with pytest.raises(ArchiveError, match="SOURCE_CONFLICT|HASH_MISMATCH"):
        adapter(transport).acquire_and_archive(request, store, ids, evidence=evidence)
    assert transport.calls == []
    assert load_archive(original.directory).manifest == original.manifest
    assert load_archive(target).manifest == other.manifest


def test_completed_binding_is_canonical_and_old_unbound_checkpoints_fail_closed(tmp_path):
    store, source = archive(tmp_path)
    request = source.manifest.request
    session = store.root / "sessions" / request.request_id / "initial"
    checkpoint = json.loads((session / "completed.json").read_bytes())
    binding = json.loads(source.read("acquisition.json"))
    assert binding["request_id"] == request.request_id == binding["request_sha256"]
    assert binding["identities_sha256"] == digest(source.read("identities.json"))
    assert binding["evidence_sha256"] == digest((session / "evidence.json").read_bytes())
    assert checkpoint["binding_sha256"] == digest(source.read("acquisition.json"))
    transport = Transport([])
    client = adapter(transport)
    assert client.acquire_and_archive(request, store, normalize_identities([identity()])) == source
    (session / "completed.json").write_bytes(canonical({"revision": source.manifest.revision}))
    with pytest.raises(ArchiveError, match="SOURCE_CONFLICT"):
        client.acquire_and_archive(request, store, normalize_identities([identity()]))
    assert transport.calls == []


def test_completed_evidence_cannot_be_replaced_or_referenced_without_its_hash(tmp_path):
    evidence = (ArchiveInput("action.json", "corporate_actions", b"original", "fixture://action"),)
    store, source = archive(tmp_path, evidence=evidence)
    transport = Transport([])
    with pytest.raises(ArchiveError, match="REVISION_CONFLICT"):
        adapter(transport).acquire_and_archive(
            source.manifest.request,
            store,
            normalize_identities([identity()]),
            evidence=(replace(evidence[0], source_ref="fixture://changed"),),
        )
    assert transport.calls == []


def repack_archive(tmp_path, source, changes):
    """Every test mutation has valid file/manifest hashes; only semantic binding is wrong."""
    contents = {item.path: source.read(item.path) for item in source.manifest.files} | changes
    manifest = replace(
        source.manifest,
        files=tuple(
            replace(item, sha256=digest(contents[item.path]), size_bytes=len(contents[item.path]))
            for item in source.manifest.files
        ),
    )
    directory = tmp_path / manifest.revision
    directory.mkdir(parents=True)
    for path, content in contents.items():
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    encoded = canonical(asdict(manifest))
    (directory / "manifest.json").write_bytes(encoded)
    (directory / "manifest.sha256").write_text(digest(encoded) + "\n")
    return directory


@pytest.mark.parametrize(
    "damage",
    [
        "swapped_refs",
        "swapped_hashes",
        "missing_retry",
        "duplicate_retry",
        "wrong_file",
        "successful_retry",
        "nonretryable_status",
        "boolean_status",
        "terminal_status",
        "orphan_retry",
        "retryable_status_mismatch",
        "wrong_ordinal",
        "receipt_downgrade",
    ],
)
def test_retry_evidence_must_match_attempts_even_with_valid_global_hashes(tmp_path, damage):
    transport = Transport(
        [HTTPResponse(429, b"rate limit"), HTTPResponse(503, b"unavailable"), page()]
    )
    store = ArchiveStore(tmp_path / "original")
    source = adapter(transport, retries=2).acquire_and_archive(
        source_request(), store, normalize_identities([identity()])
    )
    assert audit_archive(source)["status"] == "archive_complete"
    receipt = json.loads(source.read("pages/0000.receipt.json"))
    attempts, retries = receipt["attempts"], receipt["retry_responses"]
    if damage == "swapped_refs":
        retries.reverse()
    elif damage == "swapped_hashes":
        attempts[0]["response_sha256"], attempts[1]["response_sha256"] = (
            attempts[1]["response_sha256"],
            attempts[0]["response_sha256"],
        )
    elif damage == "missing_retry":
        retries.pop()
    elif damage == "duplicate_retry":
        retries[1] = retries[0]
    elif damage == "wrong_file":
        retries[0] |= {"path": "pages/0000.json", "sha256": digest(source.read("pages/0000.json"))}
    elif damage == "successful_retry":
        attempts[0]["status"] = 200
    elif damage == "nonretryable_status":
        attempts[0]["status"] = 403
    elif damage == "boolean_status":
        attempts[0]["status"] = False
    elif damage == "terminal_status":
        attempts[-1]["status"] = 429
    elif damage == "retryable_status_mismatch":
        attempts[0]["status"] = 503
    elif damage == "wrong_ordinal":
        attempts[0]["ordinal"] = 1
    elif damage == "receipt_downgrade":
        receipt.pop("schema_version")
        for attempt in attempts:
            attempt.pop("ordinal")
        for retry in retries:
            retry.pop("ordinal")
            retry.pop("status")
    elif damage == "orphan_retry":
        attempts.pop(0)
        retries.pop(0)
        for ordinal, attempt in enumerate(attempts):
            attempt["ordinal"] = ordinal
        retries[0] |= {"path": "pages/0000.attempt-00.bin", "ordinal": 0}
        directory = repack_archive(
            tmp_path / "bad",
            source,
            {
                "pages/0000.receipt.json": canonical(receipt),
                "pages/0000.attempt-00.bin": source.read("pages/0000.attempt-01.bin"),
            },
        )
        with pytest.raises(ArchiveError, match="SOURCE_CONFLICT"):
            load_archive(directory)
        return
    directory = repack_archive(
        tmp_path / "bad", source, {"pages/0000.receipt.json": canonical(receipt)}
    )
    with pytest.raises(ArchiveError, match="SOURCE_CONFLICT"):
        load_archive(directory)


def test_legacy_standalone_archives_replay_but_cannot_satisfy_a_bound_completed_session(tmp_path):
    store, source = archive(tmp_path)
    inputs = []
    for item in source.manifest.files:
        if item.role == "acquisition_session":
            continue
        content = source.read(item.path)
        if item.role == "receipt":
            receipt = json.loads(content)
            receipt.pop("schema_version")
            for attempt in receipt["attempts"]:
                attempt.pop("ordinal")
            for retry in receipt["retry_responses"]:
                retry.pop("ordinal")
                retry.pop("status")
            content = canonical(receipt)
        inputs.append(ArchiveInput(item.path, item.role, content, item.source_ref))
    legacy = store.commit(
        request=source.manifest.request,
        layer="source",
        inputs=tuple(inputs),
        acquired_start_at=source.manifest.acquired_start_at,
        acquired_end_at=source.manifest.acquired_end_at,
        adapter_version=source.manifest.adapter_version,
        writer_version=source.manifest.writer_version,
    )
    assert audit_archive(load_archive(legacy.directory))["market_data_verified"] is False
    assert len(tuple(iter_raw_bars(normalize_archived_snapshot(legacy, store)))) == 1
    checkpoint = (
        store.root / "sessions" / source.manifest.request.request_id / "initial" / "completed.json"
    )
    checkpoint.write_bytes(
        canonical(json.loads(checkpoint.read_bytes()) | {"revision": legacy.manifest.revision})
    )
    transport = Transport([])
    with pytest.raises(ArchiveError, match="SOURCE_CONFLICT"):
        adapter(transport).acquire_and_archive(
            source.manifest.request, store, normalize_identities([identity()])
        )
    assert transport.calls == []
