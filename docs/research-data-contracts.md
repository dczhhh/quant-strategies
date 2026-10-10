# Offline research data contracts (Issue #5, stage 5A)

This implements the **5A data contracts and normalization** in
[Issue #5 specification V2](https://github.com/dczhhh/quant-strategies/issues/5#issuecomment-6096588150).
The package is `quant_constraints.research.data`; it has no provider adapter, engine entry point,
account mutations or strategy decisions. It depends on the PR #4 package layout, without changing
its broker, execution, fees, settlement or risk behavior.

**Issue #5 remains open and blocks real historical stock research.** These contracts validate
structure and declarations. A structurally valid `RawBar` does not prove that its prices are raw.
Source references, declared units and historical publication times are not authenticated here.
The contracts deliberately have no `verified` flag. No current `DataFeed`/`MarketContext` input
is made trustworthy or intercepted by this stage.

## Contracts and clocks

Every record has `schema_version="research_data_v2"`. Direct construction and mapping normalization
perform the same structural checks. All records are frozen dataclasses; accepted timestamps are
timezone-aware and normalized to UTC. `source_timezone` retains the source's IANA timezone.

| Contract | Required content | Structural rejection |
| --- | --- | --- |
| `SourceProvenance` | Provider, dataset ID/revision, source partition ID/reference, original record ID/version, `ingested_at`, source URI, source timezone | Empty identifiers/partition references, naive timestamps, invalid timezone/version |
| `RawBar` | Stable security ID, contemporary symbol/venue, currency, product ID, start/end/available clocks, OHLCV, price/share/volume units, source | Nonfinite/boolean/string numbers, nonpositive prices, negative volume, malformed OHLC, adjusted declarations |
| `SignalBar` | Bar fields plus separate product ID, referenced raw product ID; adjusted products require adjustment version | Overwriting the raw product ID, incomplete or inconsistent adjustment declarations |
| `SecurityIdentity` | Stable ID, symbol/venue/currency, `[effective_from, effective_until)`, explicit listing/rename, source | Empty/inverted intervals, unsupported merger/delisting |
| `CorporateActionRecord` | Security/event ID, kind, effective date, explicit historical-public-time value or `None`, evidence reference or `None`, event version, source, currency, kind-specific terms | Conflicting terms, unsupported actions, missing public-time references, invalid revisions/dates |

For bars, `bar_start_at < bar_end_at <= available_at <= ingested_at`. The end is the exclusive
interval boundary, not permission to decide before the bar becomes available. Zero volume and
fractional shares in volume are representable; interpretation of missing/zero bars and vendor
volume policy belongs to later validation.

For corporate actions:

- `effective_date` is the event's historical effective fact.
- `historically_known_at` is the claimed historical publication time, with `knowledge_evidence`.
  It can precede or follow the effective date, but cannot follow this ingestion. When public time
  cannot be established, **both fields must be explicitly `None`**; normalization never substitutes
  the effective date or ingestion time. A reference is a declaration pending future verification.
- `source.ingested_at` is this acquisition's clock. A 2026 acquisition is never silently turned into
  2020 strategy knowledge. The stage does not perform PIT snapshots or expose events to a strategy.
- `event_version` and `revision_of` preserve the revision chain. Version one has no predecessor;
  later versions name the immediately preceding version. `cancelled` preserves a tombstone along
  with its terms. Cross-record revision selection and PIT conversion belong to 5D.

Only ordinary USD splits (positive **new/old** ratio, including reverse splits), cash dividends and
cash credits are representable in this supported scope. Dividend `effective_date` equals `ex_date`;
record/payable dates are optional metadata and **never credit confirmation**. Credit records require
their parent entitlement, credit date/net amount and either `broker_confirmation` or
`simulated_assumption`, plus its reference. A broker confirmation cannot claim an ingestion or
known time before the credited date in the source timezone. Unknown public time remains unknown;
future simulated credits remain explicitly hypothetical. No entitlement or cash is created here.

## Units and identity

`RawBar` requires `price_basis=raw`, `share_unit=as_traded`,
`volume_unit=as_traded_shares`, and no adjustment version. `SignalBar` independently declares
`raw`, `split_adjusted` or `total_return`. Adjusted signals use the adjusted share unit and a
versioned adjustment declaration. Their volume may explicitly remain as-traded or be split-adjusted;
normalization never multiplies price/volume or assumes a vendor's adjustment policy.

`validate_price_risk_input(execution, risk)` checks that price inputs for ATR/absolute stops have
raw prices, execution share units, matching security/venue/currency/interval and a matching raw
product reference for signal inputs. The contemporary symbol must match as well; a valid rename
has separate products within the old and new identity intervals, never mixed within one bar.
Risk data cannot have a later availability clock. This checks
declared units only; it does not calculate ATR, validate factor authenticity or change risk rules.

`IdentityMap` sorts immutable records and rejects overlapping assignments, including duplicates.
A rename requires a contiguous predecessor with the same stable ID, venue and currency and an
explicit rename transition. Ticker reuse across different stable IDs is currently rejected even
when the intervals do not overlap. Merger/delisting mappings are also unsupported. The method
`resolve_effective` resolves **effective facts**, not information known to a historical strategy;
it is not an as-of/PIT API. Rename representation does not enable broker ticker migration.

After all interval/rename checks, the map builds a private read-only `(symbol, venue)` index.
Both interval starts and records are tuples inside `MappingProxyType`. Queries use `bisect_right`
within that group and inspect at most one candidate, including half-open ends and gaps; unrelated
securities are not scanned. Query complexity is expected O(1) for grouping plus O(log K) for K
intervals of that symbol/venue, rather than O(M) across the whole pool. The derived index is
excluded from equality/repr; copy/pickle rebuild it from canonical entries. For serialization,
persist the public entries, not the private cache. This remains effective-fact lookup, not PIT.

### Identity market, consolidated coverage and SMART routing

Following the [SMART routing review decision](https://github.com/dczhhh/quant-strategies/pull/6#issuecomment-6097465498),
`venue` in `SecurityIdentity`, `RawBar` and `SignalBar` identifies the security's contemporary
**listing/identity market**. For example, a Nasdaq-listed security's consolidated bar can use
`venue="XNAS"` to associate it with its historical identity. This does not assert that every trade
in that bar occurred on Nasdaq, or identify the final execution destination of an IBKR SMART order.
Consolidated cross-market trades do not relax stable-ID, symbol, half-open interval or rename checks.
Unknown SMART execution destinations do not make an otherwise valid consolidated bar inadmissible.
The v2 fields and normalization behavior remain unchanged; coverage is not inferred from `venue`.

The research acquisition default for 5B/5C is qualified US consolidated trades OHLCV across
Tape A/B/C, including eligible cross-exchange and FINRA TRF-reported trades according to the
supplier's documented policy. Prioritize suppliers' consolidated coverage of NYSE/Nasdaq/Cboe,
IEX/MEMX and TRF, then assess security coverage, historical depth, RTH/early-close sessions,
raw prices, PIT corporate actions, quotes/NBBO, licensing and cost. This does not require
independent order books from every exchange or authorize an unverified source.

5B must record the following in the dataset/immutable manifest, bound to archived source evidence;
these are **future manifest requirements, not new v2 record fields**:

| Declaration | Required meaning |
| --- | --- |
| `market_scope` | Explicit `us_consolidated` or `single_venue`; never infer full-market coverage from the identity market |
| `coverage` / `exclusions` | Actual venues/reporting facilities and included/excluded TRF, opening/closing auctions, odd lots and out-of-session trades, with the supplier's definitions |
| `source_feed` | Documented originating feed and aggregation policy; identify overlapping SIP and venue-specific inputs |
| `tape` | Applicable Tape A/B/C coverage, without treating a listing tape as the execution venue |
| `session_filter` | Actual included sessions and RTH/early-close filtering policy |

Unknown source scope must fail closed at the later validation/entry gates. Single-venue data cannot
be relabeled consolidated; missing coverage is not zero volume. Combining SIP and venue-specific
inputs must avoid double counting. The current 5A contracts do not authenticate or enforce these
manifest declarations, so Issue #5's research block remains in force.

Minimum 5B/5C acceptance regressions must distinguish consolidated and single-venue inputs for the
same NYSE/Nasdaq-listed security: only qualified consolidated input can support an indicator
labeled full-market RVOL/volume, without discarding trades because they occurred away from its
listing market. Preserve historical rename/half-open identity cases; require explicit missing
coverage disclosures and reject overlapping-feed double counts. Retain raw-price, split-factor,
availability and source/hash checks regardless of the unknown SMART destination.

Reliable historical NBBO may inform spread/reachability assumptions, but neither NBBO nor bar
high/low guarantees a fill. Keep the existing [IBKR Pro Tiered fee proxies and 2/3/5 bps slippage
scenarios](us-cash-concentrated.md) explicit; do not infer venue fees/rebates or claim exact broker
fills from consolidated bars. Optional execution-venue modeling belongs to a later scope with
reliable venue-specific trades and a microstructure/directed-order simulation requirement.

## Source record namespace and schema migration

`record_id` is unique only inside
`(provider, dataset_id, dataset_revision, source_partition_id, record_version)`.
`source_partition_id` and `source_partition_ref` are required, nonempty declarations; there is no
implicit global scope and no default inferred from a ticker. The reference identifies the original
file/request or the supplier's documented globally unique partition. A later 5B adapter must derive
and archive this namespace from the actual response/request/file evidence and bind it to the
immutable manifest and file hashes; 5A checks only the presence and consistency of declarations.

Two files may each contain `record_id="1"` when their declared partitions differ. Within the same
partition, duplicate IDs/versions still reject across different securities. A partition ID must
refer to one consistent reference within a normalized batch; disagreement yields
`PROVENANCE_CONFLICT`. A different partition never legitimizes duplicate bar intervals.
Source references are declarations pending 5B/5C verification, not authenticity certificates.

The first pre-review schema (`research_data_v1`) did not require a partition. This mandatory contract
change increments the schema and normalizer to v2. Explicit v1 inputs reject with
`UNSUPPORTED_SCHEMA`; missing partition fields reject with `MISSING_FIELD` and null/blank fields
with `INVALID_VALUE`. Rebuild earlier synthetic fixtures with an explicitly chosen namespace;
do not auto-upgrade a real source without its partition evidence. No released engine API changes.

## Strict offline normalization

`normalize_record(RecordType, mapping)` accepts the canonical fields, ISO date/aware datetime strings
and enum values. It copies the mapping and rejects unknown fields, missing required fields and
unsupported schemas. Numbers must already be finite numeric values; there is no string conversion,
timezone guessing, forward filling, price repair, adjustment or file/network I/O. Numeric values
normalize to floats and timestamps to UTC; original wire bytes belong to 5B archival storage.
The normalizer version is `research_normalizer_v2`.

`normalize_identities(...)` builds the interval map. `normalize_raw_bars(..., identities)` and
`normalize_signal_bars(..., identities)` check each bar against the map and reject bars crossing
an identity boundary, duplicate intervals/source records and overlapping intervals. Raw products
cannot compete for the same security/venue interval. Signal products retain their separate IDs.
The returned tuples are chronologically sorted independent of input order and timezone notation.
An empty batch is structurally valid; this is not a claim of data coverage. Batch failure returns
no partially normalized collection and leaves the supplied mappings untouched.

```python
from quant_constraints.research.data.contracts import ContractError, RawBar
from quant_constraints.research.data.normalize import normalize_record

try:
    normalize_record(RawBar, {"verified": True})
except ContractError as error:
    assert error.as_dict()["code"] == "UNKNOWN_FIELD"
```

`ContractError` exposes stable `code`, `field`, `detail` and `as_dict()` with the schema version.
Codes distinguish missing/unknown fields, invalid type/value, schema, naive/time-order errors,
OHLCV, units, duplicate/overlapping bars, missing/conflicting/unsupported identity, unsupported
action/terms, knowledge evidence and credit evidence. Multi-field mapping diagnostics are ordered
deterministically by field name; conflicting partition references have their own provenance code.
Ingestion systems can record these errors without parsing text.

## Remaining stages and validation

The authoritative scope is the linked Issue #5 V2 specification, not a vendor's raw-data label.
All tests use **synthetic offline fixtures**; no actual NVDA/AAPL or supplier data is claimed.
Run `uv run pytest tests/constraints/test_research_data_contracts.py -q -o addopts='' --no-cov`.
Full CI retains coverage, compatibility, security, artifact and ecosystem checks. The CI trigger
also includes the PR #4 branch so the independent 5A dependency PR receives the full checks.

5B still needs acquisition/archive/immutable content hashes, 5C raw authenticity and factor/unit
validation, 5D proved PIT/provider conversion, 5E hash-bound research entry and provenance output,
and 5F licensed real-history E2E acceptance. This stage provides none of those trust guarantees and
does not close Issue #5. PR #4's separate ninth-review performance work is unchanged.

### Offline performance evidence

`uv run python validation/benchmark_research_contracts.py --samples 3 --output report.json` runs
1/10/100/1000 securities with 10k/100k **total bars**, using fresh Unix workers. Both indexed and
retained linear lookups normalize identical v2 synthetic inputs, including reversed input order
and namespaced source IDs. Every output field is digested and compared outside the timed region.
The benchmark measures the full normalization call, excluding fixture setup; peak process RSS
includes imports, inputs, index and normalized output before digesting. This is not a comparison
between whole v1 and v2 pipelines, nor a real-data or engine throughput claim.

CI runs one sample per case and retains `research-contract-performance` as an artifact. Ordinary
regressions independently gate exact tuples/error evidence, boundaries/gaps, read-only caches and
one candidate read per query for the 1/10/100/1000-security pools. Absolute timing is evidence only,
so runner noise does not become a flaky acceptance threshold. A separate regression spies on
binary search over 1000 intervals of one symbol and verifies no unrelated group is searched.

The [three-sample local report](research-contract-performance.json) binds the measured source files
by SHA-256 and records all observations and normalized-output digests. Python 3.12.14, Linux x86-64,
same shared container, fresh subprocesses per sample; figures below are medians for time/throughput
and maximum observed RSS. Both variants construct the current map (the linear one ignores its
cache), isolating query behavior; this does not estimate cache-free v1 memory savings.

| Securities | Total bars | Indexed seconds | Linear seconds | Indexed bars/s | Indexed / linear peak RSS MiB |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 10000 | 0.215 | 0.208 | 46468 | 142.4 / 142.3 |
| 1 | 100000 | 1.933 | 1.896 | 51744 | 304.8 / 304.7 |
| 10 | 10000 | 0.212 | 0.217 | 47210 | 142.6 / 142.3 |
| 10 | 100000 | 2.099 | 2.148 | 47638 | 305.4 / 305.5 |
| 100 | 10000 | 0.211 | 0.234 | 47442 | 142.3 / 142.4 |
| 100 | 100000 | 2.092 | 2.141 | 47807 | 304.3 / 304.0 |
| 1000 | 10000 | 0.210 | 0.307 | 47622 | 143.3 / 143.3 |
| 1000 | 100000 | 2.113 | 3.013 | 47324 | 304.7 / 305.2 |

In this run, a 1000-security/100k-bar batch took about 30% less time with indexed lookup.
Small pools show fixed lookup overhead/noise and are not uniformly faster. Full normalization
still retains/sorts the input/output and tracks duplicate records; it remains batch-memory-bound
with O(N log N) sorting, independent of the removal of unrelated identity scans. These synthetic
observations do not establish universal hardware performance or streaming scalability.
