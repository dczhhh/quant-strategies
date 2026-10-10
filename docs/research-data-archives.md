# Research acquisition and immutable archives (Issue #5B)

This implements the small-window archive/replay infrastructure in
[5B execution specification V3](https://github.com/dczhhh/quant-strategies/issues/5#issuecomment-6097662839),
stacked on PR #6's [v2 contracts](research-data-contracts.md). The public opt-in interface is
`quant_constraints.research.data.ingestion`; neither importing the existing contracts nor using
the existing Engine enables ingestion or changes account behavior.

**Archive integrity is not market-data authenticity. Issue #5 remains open.** All CI samples are
synthetic. No supplier subscription, real-data license, historical API access or independent
raw/PIT evidence has been established by this implementation. No real market data was downloaded
or added to the repository. There is no `VerifiedDataset`, trusted Engine entry or trading signal.

## Request and acquisition contract

`SourceRequest` freezes a stable security ID, historical symbol/listing venue, USD currency,
half-open UTC range, frequency (`1m` or `1d`), session filter, source/coverage and access declarations.
It requires `adjusted=False`, the Massive aggregate API v2 and the 5A schema/normalizer v2.
Canonical JSON determines `request_id`; no credentials or arbitrary request headers are fields.
Requests are limited to seven UTC days. Minute boundaries must be exact; daily boundaries must
be New York midnight, and daily vendor aggregates cannot be relabeled RTH-only.

`AccessDeclaration` explicitly distinguishes synthetic tests from `licensed_private` archives,
records the plan/license reference, archival permission and allowed history/frequencies, and
rejects requests outside that declared range. It is a caller's declaration, not a license verifier.
Before real acquisition, confirm subscription history, access, rate limits and storage/redistribution
terms. Keep licensed data in private storage outside the Git checkout; never commit it or keys.

`CoverageDeclaration` retains `us_consolidated` versus `single_venue`, source feed/definition,
Tape A/B/C, declared venues, trade-condition and historical-correction policies, and quote/NBBO
availability. TRF, auctions, odd lots and out-of-session trades each have exactly one explicit
included/excluded/unknown status. Unknown coverage is not included coverage or zero volume.
The Massive aggregate adapter only accepts declared consolidated requests; it does not fetch a
venue-specific tape or combine overlapping SIP/venue feeds. These declarations still need 5C evidence.

The adapter implements the supplier's documented
[custom-bar endpoint](https://massive.com/docs/rest/stocks/aggregates/custom-bars), with explicit
`adjusted=false`, ascending order, a recorded base-aggregate limit and millisecond query endpoints.
It sends the exclusive end minus one millisecond and records any returned bars outside the requested
range instead of using them. The response must confirm the ticker/adjustment, counts, server request
ID and success status. A terminal page at the base limit without continuation/completeness evidence
is conservatively rejected; use a larger valid limit or split the request.

`MassiveAdapter` accepts an injected transport for offline tests. Native HTTPS requires
`licensed_private` permission and `MASSIVE_API_KEY` from the environment; explicit constructor keys
are also supported for secret-manager integration. Authentication is a bearer header, never a URL
parameter. Redirects and changed origins/tickers/adjustment semantics reject. Credential-bearing
responses or pagination links reject rather than rewriting supposedly original bytes. Retained
headers are limited to content type, date and request ID; errors omit response bodies and credentials.

Timeout (up to 60 seconds), retries (up to eight), page count (up to 100), per-response bytes (16 MiB)
and total acquisition bytes (64 MiB) have bounds. Defaults are 20 seconds, three retries, 32 pages
and a conservative 12-second request interval. Retryable transport/429/selected 5xx failures use
bounded exponential backoff and bounded numeric `Retry-After`; 401/403 are permission gaps, never
empty market data. Choose an interval appropriate to the confirmed plan. Failed-response bytes
from completed retry chains are preserved alongside status/hash/time receipts. An exhausted chain
returns an `ArchiveError`; it does not create a completed empty snapshot.

## Immutable L0, derived L1 and manifests

`acquire_and_archive(request, store, identities, acquisition_id=..., evidence=...)` first binds
the request and canonical historical identity entries. A request crossing a rename boundary must
be split. Completed pages and checksummed receipts are written exclusively under a request/session
checkpoint. After interruption, the same acquisition ID reuses intact pages and continues from
their cursor. A completed ID returns its frozen snapshot without a network call. A new acquisition
ID explicitly refetches; changed bytes/clocks produce another revision and preserve the old one.
An incomplete/corrupt checkpoint fails closed; do not repair it by silently recalculating hashes.

Each new acquisition archives an `acquisition.json` (`research_acquisition_v1`) with an explicit
`acquisition_session` role. It binds the full request digest, request/acquisition IDs, identity hash
and the sorted supplementary evidence fingerprint. `completed.json` binds that session digest to
the frozen revision. Recovery checks the target manifest, identities, evidence and all saved page,
receipt and retry bytes/digests against the current session before returning it. A pointer to another
otherwise valid revision is rejected without fetching or creating a replacement archive. Different
acquisition IDs have distinct bindings even when the market bytes and acquisition clocks coincide.
Old standalone snapshots can still replay offline after integrity audit, but an old completion
pointer lacking a session binding is rejected; start a new acquisition ID instead of upgrading it
or rewriting the old files.

New receipts use `research_receipt_v1`. The ordered attempts have explicit zero-based ordinals;
each failed attempt has exactly one retry file with the same ordinal, status and response digest,
a page-specific path and matching source reference. Only the final attempt is successful and binds
the page. Extra/unreferenced retry files, missing responses, exchanged paths/hashes/statuses, bad
ordinals and downgrade to an unversioned receipt fail audit even if all file and manifest hashes
were freshly recomputed. Older standalone unversioned receipts retain positional count/path/hash/
status validation; they cannot satisfy the new bound-session format.

| Layer | Content and purpose |
| --- | --- |
| L0 source | Original successful page bytes, retry-response bytes, sanitized URLs/receipts, exact request, identity entries and optional supplementary source evidence |
| L1 normalized | Independent copies of bound L0 evidence, parent source manifest, per-security/date/frequency raw Parquet partitions and coverage report |
| Manifest | Sorted paths, roles, source references, SHA-256 and byte sizes; request/coverage/access, acquisition clocks, adapter/schema/normalizer/writer versions; L1 binds the L0 manifest digest |

`ArchiveInput` accepts opaque original corporate-action, adjustment-factor, comparison, supplier
definition and license-term files with
explicit roles and source references. All supplied evidence is hashed and carried into L1; absent
roles are reported. The current HTTP adapter acquires aggregates only. Split/adjusted comparison
acquisition, event interpretation and independent evidence selection still require their own
provider work and confirmed permissions; attaching a file does not authenticate its facts or PIT.

`ArchiveManifest` uses `research_archive_v1`; coverage reports use `research_dataset_v1`.
Its revision is the SHA-256 of its canonical bytes, stored as the directory name and a separate
`manifest.sha256`, avoiding recursive self-hashing. Unknown fields/versions reject. Files are
published without overwriting existing bytes; complete revisions are published as directories.
Paths must remain canonical and relative, and symlinks are unsupported. This local integrity model
detects accidental replacement against the bound revision; it is not an adversarial authentication
guarantee in a writable Python/filesystem environment.

`normalize_archived_snapshot` uses no network. The window is bounded and records are grouped by
one security/trade date, then passed through 5A normalization and written as deterministic Parquet
for the same frozen input and writer version. Date/time columns use canonical UTC ISO strings and
the nested source record preserves v2 fields. Polars writer version is recorded because changing
dependencies may change encoded bytes. No forward filling, manufactured bars, split adjustment,
volume rescaling or account mutation occurs. Cross-page duplicate timestamps reject, including
duplicates concealed by distinct source partitions. `source_partition_id/ref` binds the actual
request/page namespace and original file; missing supplier row IDs are explicitly represented by
the versioned `derived_page_row_timestamp_v1` algorithm (original row ordinal plus timestamp).

## Coverage, clocks and offline replay

The [supplier's aggregate definition](https://massive.com/knowledge-base/article/why-is-massives-market-data-different-from-other-providers)
and [trade eligibility description](https://www.massive.com/blog/understanding-trade-eligibility)
must be preserved/assessed before 5C. An absent bar can reflect aggregation eligibility, source
gaps, permission gaps or unknown causes. The adapter does not infer a confirmed reason from absence.
RTH coverage uses the existing NYSE calendar, including early closes and DST. Reports list actual
counts/first/last bars, session bounds, missing RTH windows with `reason=unknown`, nontrading dates,
session exclusions and returned out-of-range records. A successful empty nontrading response has
no invented prices; a short RTH session is not automatically a failed archive or zero trading.

Historical aggregates may be final retrospective corrections. Bar `available_at` therefore uses
the successful page's ingestion clock, explicitly labeled `ingestion_upper_bound_unverified`.
It never substitutes historical bar end as proven publication time. Acquisition before a completed
window/bar rejects. The report discloses corrections as `not_compared_to_prior_acquisition`; saved
versions can be compared, but the code does not prove what was known in the historical minute.

`audit_archive` checks manifest identity/digest, exact file inventory, hashes/lengths, the bound
request, response/receipt relationships and clocks, and the L1 parent manifest. Its result is
`archive_complete`, with `market_data_verified=False`. The normalized coverage report says
`archive_ready`, with the same false value and missing-evidence roles. Neither status authorizes
historical trading, proves raw prices, or qualifies unproven scope for full-market RVOL.

`load_archive(path)` and `iter_raw_bars(dataset)` replay offline. Batch entry points audit all bound
files before transforming/yielding, then each read rechecks its manifest and target bytes. L1 contains
its own bound source copies, so changing an external L0 revision does not rewrite an already frozen
L1; changing any copy inside L1 invalidates that L1. Independent revision directories remain intact.

```python
from pathlib import Path
from quant_constraints.research.data.ingestion import (
    ArchiveStore, MassiveAdapter, audit_archive, load_archive,
    normalize_archived_snapshot, iter_raw_bars,
)

# Prepare SourceRequest with confirmed permissions/coverage, and the historical IdentityMap.
# Native acquisition is explicit; it is not run by CI or an Engine constructor.
# source = MassiveAdapter().acquire_and_archive(request, store, identities)
# dataset = normalize_archived_snapshot(source, store)

# Replay an existing revision without a supplier connection or API key.
# dataset = load_archive(Path("/private/archive/normalized/<revision>"))
# report = audit_archive(dataset)              # archive integrity only
# for bar in iter_raw_bars(dataset):
#     pass                                    # no account/strategy calls
```

Run `uv run pytest tests/constraints/test_research_archive.py -q -o addopts='' --no-cov` for the
synthetic retry/pagination/resume, hashing/revision, scope, identity, session, missing-bar and
credential regressions. Full CI and Ecosystem remain mandatory. A licensed real-data smoke test
and provider evidence are still outstanding; 5C raw/factor verification, 5D PIT/provider conversion,
5E enforced entry and 5F authorized real-history E2E remain separate acceptance stages.

## Documentation destination checks

Strict MkDocs, internal destinations/anchors and external guide links remain merge gates. External
checks in this fork use `validation/check_research_documentation_links.py`, preserving the imported
upstream checker and retained evidence byte-for-byte. External
429/5xx/timeout failures have at most three attempts per channel, 15-second timeouts and bounded
backoff (numeric `Retry-After` up to 10 seconds). A webpage 404/410 is a missing destination and
never becomes a pass through a fallback. Authentication/access errors also remain blocking.

After an exhausted transient failure on a canonical GitHub blob link, the checker may use the
[official Contents API](https://docs.github.com/en/rest/repos/contents?apiVersion=2022-11-28).
The repository, exact ref and path must agree with a regular-file response, its HTML identity,
Git blob URL, SHA and size. Direct main/master refs, full commit SHAs and slash refs encoded in a
single URL segment are supported; other ambiguous mappings are not guessed. The API request stays
on `api.github.com` and cannot redirect a credential. CI supplies its existing read-only token
only to that API; local public checks can run unauthenticated.

The CLI logs each result as `verified_present`, `verified_missing` or `temporarily_unverifiable`,
including HTTP attempts and any official repository/ref/path/blob evidence. An API-backed presence
result proves the linked file exists at that scope; it does not claim the GitHub webpage recovered.
If both channels are unavailable, the metadata identifies another file/ref, or the API returns
404/410, Documentation fails and the final merge gate stays blocked. None of these checks certify
market-data authenticity or remove Issue #5's research blocker.
