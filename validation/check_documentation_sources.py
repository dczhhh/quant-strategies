"""Verify reviewed documentation copies, without certifying live source availability."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

# Changing a source or its provenance requires a new review of this pin.
MANIFEST_SHA256 = "e4ab18e81ce950b4b1297fe8a77b085e1a6299b600ec140b4eb6cd7d6fce2510"
SOURCE_URLS = {
    "bls-cpi.txt": "https://www.bls.gov/schedule/news_release/cpi.htm",
    "ca-cn-treaty.txt": "https://www.canada.ca/en/department-finance/programs/tax-policy/tax-treaties/country/china-agreement-1986.html",
    "sec-fee-2026.txt": "https://www.sec.gov/files/rules/other/2026/34-104909.pdf",
    "sec-t2-2017.txt": "https://www.sec.gov/newsroom/press-releases/2017-68-0",
    "occ-sandy.txt": "https://infomemo.theocc.com/infomemos?date=201210&number=31464",
}


def _read(root: Path, name: str) -> bytes:
    path = root / name
    if root.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError(f"{path}: missing file or symlink")
    if path.stat().st_size > 2_000_000:
        raise ValueError(f"{path}: oversized copy")
    return path.read_bytes()


def check_sources(sources: Path, site: Path) -> int:
    """Require the reviewed manifest, source bytes and identical rendered copies."""
    manifest_bytes = _read(sources, "manifest.json")
    if hashlib.sha256(manifest_bytes).hexdigest() != MANIFEST_SHA256:
        raise ValueError("Source manifest differs from its reviewed pin")
    if {path.name for path in sources.iterdir()} != {*SOURCE_URLS, "manifest.json"}:
        raise ValueError("Source directory contains missing or unexpected entries")
    manifest = json.loads(manifest_bytes)
    entries = manifest["entries"]
    if manifest["version"] != "reviewed_document_sources_v1" or len(entries) != len(SOURCE_URLS):
        raise ValueError("Unsupported source manifest")
    if {entry["path"]: entry["source_url"] for entry in entries} != SOURCE_URLS:
        raise ValueError("Source identities differ from reviewed URLs")
    rendered = site / "sources"
    if _read(rendered, "manifest.json") != manifest_bytes:
        raise ValueError("Rendered source manifest differs from the reviewed manifest")
    for entry in entries:
        data = _read(sources, entry["path"])
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError(f"{entry['path']}: source copy differs from its reviewed hash/size")
        if _read(rendered, entry["path"]) != data:
            raise ValueError(f"{entry['path']}: rendered copy differs from its reviewed source")
    return len(entries)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True, type=Path)
    args = parser.parse_args()
    try:
        count = check_sources(Path(__file__).resolve().parents[1] / "docs/sources", args.site)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Documentation source verification failed: {error}\n")
    print(
        f"Verified {count} pinned documentation copies and their rendered bytes. "
        "Live URL availability remains unverified; no current-law, tax identity or PIT certification."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
