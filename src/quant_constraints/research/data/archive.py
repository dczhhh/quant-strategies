"""Append-only, content-addressed local archives with fail-closed offline reads."""

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .archive_contracts import (
    ArchiveFile,
    ArchiveManifest,
    SourceRequest,
    canonical,
    digest,
    parse_time,
    reject,
)


@dataclass(frozen=True, slots=True)
class ArchiveInput:
    path: str
    role: str
    content: bytes
    source_ref: str

    def __post_init__(self):
        if not isinstance(self.content, bytes):
            reject("INVALID_REQUEST", "content", "Archive inputs must be original bytes")
        ArchiveFile(
            path=self.path,
            role=self.role,
            sha256=digest(self.content),
            size_bytes=len(self.content),
            source_ref=self.source_ref,
        )


@dataclass(frozen=True, slots=True)
class ArchivedSnapshot:
    directory: Path
    manifest: ArchiveManifest

    def read(self, path: str) -> bytes:
        """Check the manifest and requested bytes; batch entry points audit all files once."""
        encoded = canonical(asdict(self.manifest))
        try:
            if (
                safe_path(self.directory, "manifest.json").read_bytes() != encoded
                or safe_path(self.directory, "manifest.sha256").read_text(encoding="ascii").strip()
                != digest(encoded)
                or self.directory.name != self.manifest.revision
            ):
                reject("HASH_MISMATCH", "manifest", "Manifest changed before file read")
        except (OSError, UnicodeError):
            reject("HASH_MISMATCH", "manifest", "Manifest is unreadable")
        if path not in {item.path for item in self.manifest.files}:
            reject("PATH_INVALID", "path", "File is not bound by this manifest")
        try:
            content = safe_path(self.directory, path).read_bytes()
        except OSError:
            reject("HASH_MISMATCH", "file", "Bound file is unreadable")
        item = next(item for item in self.manifest.files if item.path == path)
        if digest(content) != item.sha256 or len(content) != item.size_bytes:
            reject("HASH_MISMATCH", "file", "File changed while being read")
        return content


def safe_path(directory: Path, path: str) -> Path:
    candidate = directory / path
    if directory.is_symlink() or any(part.is_symlink() for part in [candidate, *candidate.parents]):
        reject("PATH_INVALID", "path", "Archive symlinks are unsupported")
    if not candidate.resolve().is_relative_to(directory.resolve()):
        reject("PATH_INVALID", "path", "File escapes archive directory")
    return candidate


def write_once(path: Path, content: bytes) -> None:
    """Publish a fully written file using an exclusive hard-link, never overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".archive-write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != content:
                reject("REVISION_CONFLICT", "file", "Existing bytes cannot be replaced")
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_archive(directory: Path) -> ArchivedSnapshot:
    directory = Path(directory)
    manifest_path = safe_path(directory, "manifest.json")
    try:
        content = manifest_path.read_bytes()
        expected = safe_path(directory, "manifest.sha256").read_text(encoding="ascii").strip()
        if digest(content) != expected:
            reject("HASH_MISMATCH", "manifest", "Manifest bytes changed")
        manifest = ArchiveManifest.from_dict(json.loads(content))
    except (OSError, UnicodeError, json.JSONDecodeError):
        reject("HASH_MISMATCH", "manifest", "Manifest or its digest is missing/malformed")
    if canonical(asdict(manifest)) != content or manifest.revision != directory.name:
        reject("REVISION_CONFLICT", "manifest", "Manifest does not match its canonical revision")
    snapshot = ArchivedSnapshot(directory, manifest)
    audit_archive(snapshot)
    return snapshot


def audit_archive(snapshot: ArchivedSnapshot) -> dict:
    """An integrity report, explicitly not a market-data truth certificate."""
    if not isinstance(snapshot, ArchivedSnapshot):
        reject("INVALID_REQUEST", "snapshot", "Expected an archived snapshot")
    directory, manifest = snapshot.directory, snapshot.manifest
    encoded = canonical(asdict(manifest))
    try:
        if (
            safe_path(directory, "manifest.json").read_bytes() != encoded
            or safe_path(directory, "manifest.sha256").read_text(encoding="ascii").strip()
            != digest(encoded)
            or directory.name != manifest.revision
        ):
            reject("HASH_MISMATCH", "manifest", "Snapshot metadata no longer matches its manifest")
        expected = {item.path for item in manifest.files} | {"manifest.json", "manifest.sha256"}
        actual = {
            item.relative_to(directory).as_posix()
            for item in directory.rglob("*")
            if item.is_file() or item.is_symlink()
        }
        if actual != expected:
            reject("HASH_MISMATCH", "files", "Archive inventory differs from its manifest")
        for item in manifest.files:
            path = safe_path(directory, item.path)
            hasher = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(block)
                    hasher.update(block)
            if size != item.size_bytes or hasher.hexdigest() != item.sha256:
                reject("HASH_MISMATCH", "files", "Archived content or byte length changed")
        request_file = next(item for item in manifest.files if item.role == "request")
        if safe_path(directory, request_file.path).read_bytes() != canonical(
            asdict(manifest.request)
        ):
            reject("SOURCE_CONFLICT", "request", "Archived request differs from manifest request")
        indexed = {item.path: item for item in manifest.files}
        for item in manifest.files:
            if item.role != "response":
                continue
            receipt_file = indexed.get(item.path.removesuffix(".json") + ".receipt.json")
            if receipt_file is None or receipt_file.role != "receipt":
                reject("SOURCE_CONFLICT", "receipt", "An original response has no bound receipt")
            try:
                receipt = json.loads(safe_path(directory, receipt_file.path).read_bytes())
                if (
                    receipt["sha256"] != item.sha256
                    or receipt["url"] != item.source_ref
                    or receipt["status"] != 200
                ):
                    reject("SOURCE_CONFLICT", "receipt", "Receipt disagrees with original response")
                previous = manifest.acquired_start_at
                for attempt in receipt["attempts"]:
                    start, end = parse_time(attempt["started_at"]), parse_time(attempt["ended_at"])
                    if not previous <= start <= end <= manifest.acquired_end_at:
                        reject(
                            "SOURCE_CONFLICT",
                            "receipt",
                            "Receipt clocks escape acquisition coverage",
                        )
                    previous = end
                if (
                    not receipt["attempts"]
                    or receipt["attempts"][-1]["status"] != 200
                    or receipt["attempts"][-1]["response_sha256"] != item.sha256
                ):
                    reject(
                        "SOURCE_CONFLICT",
                        "receipt",
                        "Terminal attempt does not bind the successful page",
                    )
                for retry in receipt["retry_responses"]:
                    file = indexed.get(retry["path"])
                    if (
                        file is None
                        or file.role != "attempt_response"
                        or file.sha256 != retry["sha256"]
                    ):
                        reject(
                            "SOURCE_CONFLICT",
                            "receipt",
                            "Retry response is not bound by the manifest",
                        )
            except (json.JSONDecodeError, KeyError, TypeError, IndexError):
                reject("SOURCE_CONFLICT", "receipt", "Malformed acquisition receipt")
        if manifest.layer == "normalized":
            parents = [item for item in manifest.files if item.role == "source_manifest"]
            if len(parents) != 1 or parents[0].sha256 != manifest.source_manifest_sha256:
                reject(
                    "HASH_MISMATCH",
                    "source_manifest",
                    "Normalized content has no unique bound source manifest",
                )
    except (OSError, UnicodeError):
        reject("HASH_MISMATCH", "files", "A bound archive file is unreadable")
    return {
        "schema_version": manifest.schema_version,
        "status": "archive_complete",
        "revision": manifest.revision,
        "manifest_sha256": digest(encoded),
        "files": len(manifest.files),
        "market_data_verified": False,
        "availability_method": "ingestion_upper_bound_unverified",
        "missing_evidence_roles": sorted(
            {
                "corporate_actions",
                "adjustment_factors",
                "comparison",
                "source_definition",
                "license_terms",
            }
            - {item.role for item in manifest.files}
        ),
    }


class ArchiveStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        if self.root.is_symlink():
            reject("PATH_INVALID", "root", "Archive root cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)

    def commit(
        self,
        *,
        request: SourceRequest,
        layer: str,
        inputs: tuple[ArchiveInput, ...],
        acquired_start_at: datetime,
        acquired_end_at: datetime,
        adapter_version: str,
        writer_version: str,
        source_manifest_sha256: str | None = None,
    ) -> ArchivedSnapshot:
        records = []
        for item in inputs:
            if not isinstance(item.content, bytes):
                reject("INVALID_REQUEST", "content", "Archive inputs must contain original bytes")
            records.append(
                ArchiveFile(
                    path=item.path,
                    role=item.role,
                    sha256=digest(item.content),
                    size_bytes=len(item.content),
                    source_ref=item.source_ref,
                )
            )
        manifest = ArchiveManifest(
            request=request,
            layer=layer,
            files=tuple(records),
            acquired_start_at=acquired_start_at,
            acquired_end_at=acquired_end_at,
            adapter_version=adapter_version,
            writer_version=writer_version,
            source_manifest_sha256=source_manifest_sha256,
        )
        target = safe_path(self.root, layer + "/" + manifest.revision)
        if target.exists():
            existing = load_archive(target)
            if existing.manifest != manifest:
                reject("REVISION_CONFLICT", "manifest", "Cannot replace an existing revision")
            return existing
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".build-", dir=target.parent))
        temporary = staging / manifest.revision
        temporary.mkdir()
        try:
            for item in inputs:
                write_once(safe_path(temporary, item.path), item.content)
            encoded = canonical(asdict(manifest))
            write_once(temporary / "manifest.json", encoded)
            write_once(temporary / "manifest.sha256", (digest(encoded) + "\n").encode("ascii"))
            # Validate semantic bindings before publishing any completed revision directory.
            load_archive(temporary)
            try:
                temporary.rename(target)
            except OSError:
                if not target.exists():
                    raise
                existing = load_archive(target)
                if existing.manifest != manifest:
                    reject("REVISION_CONFLICT", "manifest", "Concurrent revision differs")
            return load_archive(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
