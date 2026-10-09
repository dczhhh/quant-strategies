# Upstream provenance

- Repository: https://github.com/ml4t/backtest
- Branch: main
- Imported commit: c1ee19c385db3cbad225822cf845832470694f2b
- Import date: 2026-10-09
- License: MIT; original copyright and LICENSE retained.

The upstream source, tests, examples, documentation, validation fixtures and commit
history are preserved. The destination's initialization history is retained by
merging the histories. No trading behavior is changed.

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
uv run pytest
```

Resolve conflicts in the repository introduction while retaining LICENSE.
