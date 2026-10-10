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

Every record has `schema_version="research_data_v1"`. Direct construction and mapping normalization
perform the same structural checks. All records are frozen dataclasses; accepted timestamps are
timezone-aware and normalized to UTC. `source_timezone` retains the source's IANA timezone.

| Contract | Required content | Structural rejection |
| --- | --- | --- |
| `SourceProvenance` | Provider, dataset ID/revision, original record ID/version, `ingested_at`, source URI, source timezone | Empty identifiers, naive timestamps, invalid timezone/version |
| `RawBar` | Stable security ID, contemporary symbol/venue, currency, product ID, start/end/available clocks, OHLCV, price/share/volume units, source | Nonfinite/boolean/string numbers, nonpositive prices, negative volume, malformed OHLC, adjusted declarations |
| `SignalBar` | Bar fields plus separate product ID, referenced raw product ID; adjusted products require adjustment version | Overwriting the raw product ID, incomplete or inconsistent adjustment declarations |
| `SecurityIdentity` | Stable ID, symbol/venue/currency, `[effective_from, effective_until)`, explicit listing/rename, source | Empty/inverted intervals, unsupported merger/delisting |
| `CorporateActionRecord` | Security/event ID, kind, effective date, explicit historical-public-time value or `None`, evidence reference or `None`, event version, source, currency, kind-specific terms | Conflicting terms, unsupported actions, unproven public-time declarations, invalid revisions/dates |

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
product reference for signal inputs. Risk data cannot have a later availability clock. This checks
declared units only; it does not calculate ATR, validate factor authenticity or change risk rules.

`IdentityMap` sorts immutable records and rejects overlapping assignments, including duplicates.
A rename requires a contiguous predecessor with the same stable ID, venue and currency and an
explicit rename transition. Ticker reuse across different stable IDs is currently rejected even
when the intervals do not overlap. Merger/delisting mappings are also unsupported. The method
`resolve_effective` resolves **effective facts**, not information known to a historical strategy;
it is not an as-of/PIT API. Rename representation does not enable broker ticker migration.

## Strict offline normalization

`normalize_record(RecordType, mapping)` accepts the canonical fields, ISO date/aware datetime strings
and enum values. It copies the mapping and rejects unknown fields, missing required fields and
unsupported schemas. Numbers must already be finite numeric values; there is no string conversion,
timezone guessing, forward filling, price repair, adjustment or file/network I/O. Numeric values
normalize to floats and timestamps to UTC; original wire bytes belong to 5B archival storage.
The normalizer version is `research_normalizer_v1`.

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
deterministically by field name. Ingestion systems can record these errors without parsing text.

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
