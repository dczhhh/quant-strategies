# Upstream provenance

- Repository: https://github.com/ml4t/backtest
- Branch: main
- Imported commit: c1ee19c385db3cbad225822cf845832470694f2b
- Import date: 2026-10-09
- License: MIT; original copyright and LICENSE retained.

The upstream source, tests, examples, documentation, validation fixtures and commit
history are preserved. The destination's initialization history is retained by
merging the histories. No trading behavior is changed.

That statement describes the import commit. The subsequent cash-account change
adds the opt-in `us_cash_equities` profile; see [its guide](docs/us-cash-account.md).
Inherited cross-framework correctness/performance reports remain immutable records
of the imported source. They have not been re-measured or certified for this fork.
Their ten source-bound publication checks are marked `upstream_evidence` and are
excluded from the current-source research suite. CI checks the original evidence
generator and all 27 tests in its three evidence modules on a detached worktree
at the immutable imported commit above. A separate fork-integrity check compares
the retained reports, comparison runners, provenance inputs and generated claim
blocks with that commit. The fork's packaging manifest and lint file list may
evolve to include its additional package; this does not rewrite archived evidence.
Running `uv run pytest -m upstream_evidence --no-cov` on modified source correctly
refuses certification until fresh evidence exists. Functional runner, engine,
accounting and artifact regression checks remain in the current-source suite;
the current public-parity and runtime CI jobs also remain mandatory. No retained
report digest or performance result was rewritten to imply certification of the
new source.

The root README adds repository-specific instructions. The import workflow is
temporary and is removed after import verification. Upstream release workflows
remain upstream tooling and are not configured for this project.

## Validation

Python 3.12; `uv sync --locked --dev`; Ruff lint and formatting; ty type checks;
pytest excluding benchmarks: 2261 passed, 9 skipped, 5 deselected; coverage 87.43%.
Skipped tests require optional comparison environments. Upstream test files and
engine source are unchanged.

## Updating

```bash
git remote add upstream https://github.com/ml4t/backtest.git
git fetch upstream main --tags
git merge upstream/main
uv sync --locked --dev
uv run pytest -m 'not benchmark and not upstream_evidence'
```

Resolve conflicts in the repository introduction while retaining LICENSE.
