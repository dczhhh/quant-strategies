"""Contracts for clean, typed, reproducible distributions."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
import yaml

_ROOT = Path(__file__).parents[2]


def _load_artifact_checker() -> ModuleType:
    path = _ROOT / "validation" / "check_artifacts.py"
    spec = importlib.util.spec_from_file_location("ml4t_artifact_checker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_namespace_package_contains_the_pep561_marker() -> None:
    assert (_ROOT / "src" / "ml4t" / "backtest" / "py.typed").is_file()


def test_build_configuration_excludes_internal_agent_material() -> None:
    config = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    excludes = set(config["tool"]["hatch"]["build"]["exclude"])
    assert {"**/AGENTS.md", "**/CLAUDE.md"} <= excludes


def test_distribution_declares_stable_development_status() -> None:
    config = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    classifiers = set(config["project"]["classifiers"])
    assert "Development Status :: 5 - Production/Stable" in classifiers
    assert not any(classifier.startswith("Development Status :: 4") for classifier in classifiers)


def test_manifest_comparison_rejects_unexpected_and_missing_files() -> None:
    checker = _load_artifact_checker()
    failures = checker._manifest_diff(
        {"approved.py", "secret.env"},
        {"approved.py", "py.typed"},
        "wheel",
    )
    assert "wheel has unexpected files: ['secret.env']" in failures
    assert "wheel is missing files: ['py.typed']" in failures


@pytest.mark.parametrize("change", ["clean", "missing_wheel", "missing_sdist", "extra", "internal"])
def test_cash_extension_is_required_without_allowing_extra_distribution_files(
    tmp_path, monkeypatch, change
):
    checker = _load_artifact_checker()
    source_files = {
        "src/ml4t/backtest/__init__.py",
        "src/quant_constraints/__init__.py",
        "src/quant_constraints/adapter.py",
    }
    for relative in source_files:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# packaged module\n")
    monkeypatch.setattr(checker, "_ROOT", tmp_path)
    monkeypatch.setattr(checker, "_PACKAGE", tmp_path / "src/ml4t/backtest")
    monkeypatch.setattr(checker, "_EXTENSION", tmp_path / "src/quant_constraints")
    monkeypatch.setattr(checker, "_TESTS", tmp_path / "tests")
    monkeypatch.setattr(checker, "_APPROVED_TEST_DATA", ())
    dist_info = "test.dist-info"
    wheel_files = {path.removeprefix("src/") for path in source_files} | {
        "ml4t/backtest/py.typed",
        *(
            f"{dist_info}/{suffix}"
            for suffix in ("METADATA", "WHEEL", "RECORD", "licenses/LICENSE")
        ),
    }
    sdist_files = source_files | {
        ".gitignore",
        "CHANGELOG.md",
        "LICENSE",
        "README.md",
        "PKG-INFO",
        "pyproject.toml",
        "src/ml4t/backtest/py.typed",
    }
    if change == "missing_wheel":
        wheel_files.remove("quant_constraints/adapter.py")
    elif change == "missing_sdist":
        sdist_files.remove("src/quant_constraints/adapter.py")
    elif change == "extra":
        wheel_files.add("unapproved_plugin.py")
    elif change == "internal":
        sdist_files.add("src/quant_constraints/AGENTS.md")
    monkeypatch.setattr(checker, "_single", lambda *_args: tmp_path / "unused")
    monkeypatch.setattr(checker, "_wheel_manifest", lambda *_args: (wheel_files, dist_info))
    monkeypatch.setattr(checker, "_sdist_manifest", lambda *_args: sdist_files)
    failures = checker.artifact_failures(tmp_path)
    assert bool(failures) == (change != "clean")
    if change.startswith("missing"):
        assert any("missing files" in failure and "adapter.py" in failure for failure in failures)
    elif change in {"extra", "internal"}:
        assert any("unexpected files" in failure for failure in failures)


def test_ci_checks_both_distribution_formats_and_reproducibility() -> None:
    payload = yaml.load(
        (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    commands = "\n".join(step.get("run", "") for step in payload["jobs"]["build"]["steps"])
    assert "uv build --out-dir dist-rebuild" in commands
    assert "validation/check_artifacts.py dist --compare dist-rebuild" in commands
    assert "uvx twine check dist/*" in commands


def test_distribution_metadata_matches_the_public_identity_contract() -> None:
    config = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]

    description = (
        "Event-driven backtesting for quantitative strategies with configurable execution, "
        "accounting, risk, and framework-parity validation."
    )
    assert project["description"] == description
    assert project["authors"] == [{"name": "Stefan Jansen", "email": "stefan@applied-ai.com"}]
    assert project["maintainers"] == [{"name": "Stefan Jansen", "email": "pm@ml4trading.io"}]
    assert {
        "finance",
        "quantitative-finance",
        "algorithmic-trading",
        "backtesting",
        "execution",
    } <= set(project["keywords"])
    assert {
        "Operating System :: Microsoft :: Windows",
        "Operating System :: MacOS",
        "Operating System :: POSIX :: Linux",
    } <= set(project["classifiers"])
    assert project["urls"] == {
        "Homepage": "https://www.ml4trading.io/",
        "Documentation": "https://www.ml4trading.io/docs/backtest/",
        "Repository": "https://github.com/ml4t/backtest",
        "Issues": "https://github.com/ml4t/backtest/issues",
        "Changelog": "https://github.com/ml4t/backtest/blob/main/CHANGELOG.md",
    }
