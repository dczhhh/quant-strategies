"""The fork must preserve evidence and validate it on the source it actually certifies."""

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location(
    "fork_evidence", ROOT / "validation/check_fork_evidence.py"
)
assert spec is not None and spec.loader is not None
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.fixture
def archive(tmp_path):
    originals = {
        "validation/RESULTS.json": b'{"original_digest": "unchanged"}',
        "README.md": b"intro\n" + checker.START + b"retained claim" + checker.END,
    }
    for path, content in originals.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return tmp_path, originals


def test_fork_intro_can_change_without_recertifying_imported_claims(archive):
    root, originals = archive
    path = root / "README.md"
    path.write_bytes(b"fork-specific introduction\n" + path.read_bytes())
    assert checker.verify_retained(root, originals) == []


@pytest.mark.parametrize("tamper", ["report", "delete", "claim", "markers"])
def test_changed_deleted_or_recertified_evidence_is_rejected(archive, tamper):
    root, originals = archive
    if tamper == "report":
        (root / "validation/RESULTS.json").write_bytes(b'{"digest": "modified_source"}')
    elif tamper == "delete":
        (root / "validation/RESULTS.json").unlink()
    else:
        (root / "README.md").write_bytes(
            checker.START + b"new certification" + (checker.END if tamper == "claim" else b"")
        )
    assert checker.verify_retained(root, originals)


def test_ci_runs_both_current_behavior_and_original_source_evidence():
    def workflow(name):
        return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)

    jobs = workflow("ci.yml")["jobs"]
    archive = jobs["archived-evidence"]
    commands = "\n".join(step.get("run", "") for step in archive["steps"])
    assert checker.UPSTREAM_COMMIT in commands
    assert "generate_parity_claims.py --check" in commands
    for name in (
        "test_lean_case_study_evidence.py",
        "test_parity_claim_generation.py",
        "test_real_strategy_runner.py",
    ):
        assert name in commands
    assert "archived-evidence" in jobs["build"]["needs"]
    coverage_commands = "\n".join(step.get("run", "") for step in jobs["coverage"]["steps"])
    assert "not benchmark and not upstream_evidence" in coverage_commands
    assert "--cov=quant_constraints" in coverage_commands
    for name in ("stable", "minimum", "prerelease"):
        steps = workflow("compatibility.yml")["jobs"][name]["steps"]
        assert any(
            "not benchmark and not upstream_evidence" in step.get("run", "") for step in steps
        )
    review = jobs["dependency-review"]
    assert "security" in review["needs"]
    assert any("needs.security.result" in step.get("run", "") for step in review["steps"])
    source_steps = workflow("security.yml")["jobs"]["scan"]["steps"]
    assert any("src/quant_constraints" in step.get("run", "") for step in source_steps)
