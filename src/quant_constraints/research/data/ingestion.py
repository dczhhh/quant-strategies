"""Offline 5A structures and 5B archives; archive integrity is not data truth."""

from .archive import ArchivedSnapshot, ArchiveInput, ArchiveStore, audit_archive, load_archive
from .archive_contracts import (
    AccessDeclaration,
    ArchiveError,
    ArchiveFile,
    ArchiveManifest,
    CoverageDeclaration,
    SourceRequest,
)
from .datasets import iter_raw_bars, normalize_archived_snapshot
from .massive import HTTPResponse, MassiveAdapter

__all__ = [
    "AccessDeclaration",
    "ArchiveError",
    "ArchiveFile",
    "ArchiveInput",
    "ArchiveManifest",
    "ArchiveStore",
    "ArchivedSnapshot",
    "CoverageDeclaration",
    "HTTPResponse",
    "MassiveAdapter",
    "SourceRequest",
    "audit_archive",
    "iter_raw_bars",
    "load_archive",
    "normalize_archived_snapshot",
]
