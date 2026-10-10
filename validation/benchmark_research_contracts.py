"""Offline normalization benchmark: indexed versus retained linear identity lookup.

No wall-clock gate. CI gates correctness and structural query work in constraints tests.
Both lookup variants use the same v2 contracts and normalization. Fresh Unix workers
report peak process RSS (including interpreter, inputs, index and normalized output).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from time import perf_counter

from quant_constraints.research.data.contracts import (
    SCHEMA_VERSION,
    ContractError,
    ErrorCode,
    IdentityMap,
    PriceBasis,
    SecurityIdentity,
    ShareUnit,
    SourceProvenance,
    VolumeUnit,
)
from quant_constraints.research.data.contracts.errors import timestamp
from quant_constraints.research.data.contracts.identities import IdentityTransition
from quant_constraints.research.data.normalize import NORMALIZER_VERSION, normalize_raw_bars

ROOT = Path(__file__).resolve().parents[1]
IMPLEMENTATION_FILES = (
    "src/quant_constraints/research/data/contracts/errors.py",
    "src/quant_constraints/research/data/contracts/identities.py",
    "src/quant_constraints/research/data/contracts/market_data.py",
    "src/quant_constraints/research/data/contracts/provenance.py",
    "src/quant_constraints/research/data/normalize.py",
    "validation/benchmark_research_contracts.py",
)


class LinearIdentityMap(IdentityMap):
    """The pre-review linear lookup, with identical effective-date semantics."""

    def resolve_effective(self, symbol: str, venue: str, at: datetime) -> SecurityIdentity:
        at = timestamp(at, "at")
        for entry in self.entries:
            if (
                entry.symbol == symbol
                and entry.venue == venue
                and entry.effective_from <= at
                and (entry.effective_until is None or at < entry.effective_until)
            ):
                return entry
        raise ContractError(
            ErrorCode.IDENTITY_MISSING, "symbol", "No identity covers this effective timestamp"
        )


def _source(partition: str, record_id: str) -> SourceProvenance:
    return SourceProvenance(
        provider="offline-synthetic",
        dataset_id="identity-benchmark",
        dataset_revision="fixture-v2",
        source_partition_id=partition,
        source_partition_ref=f"fixture://identity-benchmark/{partition}",
        record_id=record_id,
        record_version=1,
        ingested_at=datetime(2026, 10, 10, tzinfo=UTC),
        source_uri=f"fixture://identity-benchmark/{partition}/{record_id}",
        source_timezone="UTC",
    )


def _workload(assets: int, bars: int, lookup: str):
    entries = tuple(
        SecurityIdentity(
            security_id=f"ID{i:04}",
            symbol=f"S{i:04}",
            venue="XNAS",
            currency="USD",
            effective_from=datetime(2020, 1, 1, tzinfo=UTC),
            effective_until=None,
            transition=IdentityTransition.LISTING,
            source=_source("identities", f"ID{i:04}"),
        )
        for i in range(assets)
    )
    mapping = (IdentityMap if lookup == "indexed" else LinearIdentityMap)(entries)
    records = []
    base = datetime(2024, 6, 7, 13, 30, tzinfo=UTC)
    for i in range(bars):
        asset = i % assets
        start = base + timedelta(minutes=i // assets)
        records.append(
            {
                "security_id": f"ID{asset:04}",
                "symbol": f"S{asset:04}",
                "venue": "XNAS",
                "currency": "USD",
                "product_id": "raw-minutes",
                "bar_start_at": start,
                "bar_end_at": start + timedelta(minutes=1),
                "available_at": start + timedelta(minutes=1, seconds=1),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
                "volume": 100_000,
                "price_basis": PriceBasis.RAW,
                "share_unit": ShareUnit.AS_TRADED,
                "volume_unit": VolumeUnit.AS_TRADED_SHARES,
                "source": _source(f"file-{asset:04}", f"row-{i // assets}"),
            }
        )
    # Deliberately reverse input; both paths must return the same ordered product.
    records.reverse()
    return records, mapping


def _worker(assets: int, bars: int, lookup: str) -> dict:
    import resource  # Unix RSS only; this benchmark runs on the Ubuntu CI job.

    records, identities = _workload(assets, bars, lookup)
    start = perf_counter()
    normalized = normalize_raw_bars(records, identities)
    elapsed = perf_counter() - start
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_bytes = peak if sys.platform == "darwin" else peak * 1024
    # Check every normalized field, outside the timed region and memory boundary.
    digest = hashlib.sha256()
    for record in normalized:
        digest.update(repr(record).encode("utf-8"))
    return {
        "lookup": lookup,
        "assets": assets,
        "bars": bars,
        "seconds": elapsed,
        "bars_per_second": bars / elapsed,
        "peak_rss_mib": peak_bytes / (1024 * 1024),
        "output_count": len(normalized),
        "output_sha256": digest.hexdigest(),
    }


def measure(samples: int) -> dict:
    rows = []
    for assets in (1, 10, 100, 1000):
        for bars in (10_000, 100_000):
            observations = {"indexed": [], "linear": []}
            for sample in range(samples):
                # Alternate order to reduce systematic first-run effects.
                order = ("indexed", "linear") if sample % 2 == 0 else ("linear", "indexed")
                for lookup in order:
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--worker",
                            lookup,
                            "--assets",
                            str(assets),
                            "--bars",
                            str(bars),
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=600,
                    )
                    observations[lookup].append(json.loads(result.stdout))
            outputs = [result for results in observations.values() for result in results]
            if len({result["output_sha256"] for result in outputs}) != 1 or any(
                result["output_count"] != bars for result in outputs
            ):
                raise ValueError("Linear and indexed normalization produced different tuples")
            summary = {
                lookup: {
                    "median_seconds": median(r["seconds"] for r in results),
                    "median_bars_per_second": median(r["bars_per_second"] for r in results),
                    "peak_rss_mib": max(r["peak_rss_mib"] for r in results),
                }
                for lookup, results in observations.items()
            }
            row = {"assets": assets, "bars": bars, "summary": summary, "samples": observations}
            rows.append(row)
            print(json.dumps({"assets": assets, "bars": bars, "summary": summary}), flush=True)
    return {
        "report_version": 1,
        "schema_version": SCHEMA_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "samples_per_variant": samples,
        "python": sys.version,
        "platform": platform.platform(),
        "measurement": {
            "runtime": "perf_counter around normalize_raw_bars; setup and output digest excluded",
            "memory": "fresh worker peak process RSS, including imports, inputs, index and output; digest excluded",
            "baseline": "pre-review linear resolve_effective; both paths use identical v2 contracts",
            "input": "synthetic reversed minutes, round-robin securities, one interval per security",
            "correctness": "all output fields digested; CI also asserts exact tuples/errors and bounded candidate work",
            "gate": "no absolute wall-clock threshold",
        },
        "implementation_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in IMPLEMENTATION_FILES
        },
        "results": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("indexed", "linear"))
    parser.add_argument("--assets", type=int, choices=(1, 10, 100, 1000))
    parser.add_argument("--bars", type=int, choices=(10_000, 100_000))
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        if args.assets is None or args.bars is None:
            parser.error("worker needs assets and bars")
        print(json.dumps(_worker(args.assets, args.bars, args.worker)))
        return 0
    if args.samples < 1 or args.output is None:
        parser.error("measurement needs positive samples and output path")
    payload = measure(args.samples)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
