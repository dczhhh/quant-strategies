"""Tampered/missing documentation copies must block the documentation build."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location(
    "documentation_sources", ROOT / "validation/check_documentation_sources.py"
)
assert spec is not None and spec.loader is not None
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.fixture
def copies(tmp_path):
    sources = tmp_path / "docs/sources"
    site = tmp_path / "site"
    shutil.copytree(ROOT / "docs/sources", sources)
    shutil.copytree(sources, site / "sources")
    return sources, site


def test_reviewed_source_and_rendered_copies_pass(copies):
    assert checker.check_sources(*copies) == 5


@pytest.mark.parametrize("rendered", [False, True])
@pytest.mark.parametrize("name", ["manifest.json", "bls-cpi.txt", "occ-sandy.txt"])
@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_missing_or_tampered_copies_fail(copies, rendered, name, damage):
    sources, site = copies
    path = (site / "sources" if rendered else sources) / name
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b"tampered\n")
    with pytest.raises(ValueError):
        checker.check_sources(sources, site)


def test_rewriting_source_and_its_declared_hash_cannot_replace_reviewed_pin(copies):
    import hashlib

    sources, site = copies
    path = sources / "ca-cn-treaty.txt"
    data = path.read_bytes().replace(b"15 per cent", b"zero tax")
    assert data != path.read_bytes()
    path.write_bytes(data)
    manifest_path = sources / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    entry = next(e for e in manifest["entries"] if e["path"] == path.name)
    entry.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    manifest_path.write_text(json.dumps(manifest))
    shutil.copyfile(path, site / "sources" / path.name)
    shutil.copyfile(manifest_path, site / "sources/manifest.json")
    with pytest.raises(ValueError, match="reviewed pin"):
        checker.check_sources(sources, site)


@pytest.mark.parametrize("rendered", [False, True])
def test_external_symlink_copy_is_rejected(copies, tmp_path, rendered):
    sources, site = copies
    external = tmp_path / "external.txt"
    path = (site / "sources" if rendered else sources) / "bls-cpi.txt"
    external.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(external)
    except OSError:
        # Windows CI may not grant symlink creation. This is still a required failure case.
        with pytest.raises(ValueError):
            checker.check_sources(sources, site)
        return
    with pytest.raises(ValueError, match="symlink"):
        checker.check_sources(sources, site)


def test_unreviewed_source_entry_is_rejected(copies):
    sources, site = copies
    (sources / "unreviewed.txt").write_text("extra source\n")
    with pytest.raises(ValueError, match="unexpected"):
        checker.check_sources(sources, site)
