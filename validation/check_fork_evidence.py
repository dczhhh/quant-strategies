#!/usr/bin/env python3
"""Protect imported evidence without asserting that it certifies modified source."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

UPSTREAM_COMMIT = "c1ee19c385db3cbad225822cf845832470694f2b"
CLAIM_TARGETS = (
    "README.md",
    "docs/index.md",
    "docs/user-guide/profiles.md",
    "validation/README.md",
    "validation/METHODOLOGY.md",
)
START = b"<!-- parity-claims:start -->"
END = b"<!-- parity-claims:end -->"
# The lint/typecheck file list may grow as the fork adds validation tools.
EDITABLE = {"validation/release_checks.txt", *CLAIM_TARGETS}


def claim_block(content: bytes) -> bytes:
    if content.count(START) != 1 or content.count(END) != 1:
        raise ValueError("Exactly one complete parity claim block is required")
    start, end = content.index(START), content.index(END)
    if end < start:
        raise ValueError("Parity claim markers are out of order")
    return content[start : end + len(END)]


def verify_retained(root: Path, originals: Mapping[str, bytes]) -> list[str]:
    failures = []
    for relative, original in originals.items():
        path = root / relative
        if not path.is_file():
            failures.append(f"Imported evidence deleted: {relative}")
            continue
        current = path.read_bytes()
        if relative in CLAIM_TARGETS:
            try:
                if claim_block(current) != claim_block(original):
                    failures.append(f"Imported parity claim changed: {relative}")
            except ValueError as error:
                failures.append(f"{relative}: {error}")
        elif relative not in EDITABLE and current != original:
            failures.append(f"Imported evidence changed: {relative}")
    return failures


def imported_files(root: Path) -> dict[str, bytes]:
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", UPSTREAM_COMMIT, "validation"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    paths = sorted(set(result.stdout.splitlines()) | set(CLAIM_TARGETS))
    return {
        path: subprocess.run(
            ["git", "show", f"{UPSTREAM_COMMIT}:{path}"],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
        for path in paths
    }


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    failures = verify_retained(root, imported_files(root))
    if failures:
        print("\n".join(failures))
        return 1
    print(f"Imported evidence and claims unchanged from {UPSTREAM_COMMIT}")
    print("Source-bound certification is checked separately on that immutable commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
