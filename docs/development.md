# Development

Use Python 3.11+ and a dedicated environment:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Run the narrowest affected test first, then the complete release checks:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/pytest
.venv/bin/pytest --cov=aer --cov-report=term-missing
.venv/bin/python -m build
PYTHON=.venv/bin/python AER_SMOKE_VENV=/tmp/aer-wheel-smoke \
  AER_SMOKE_HOME=/tmp/aer-wheel-home scripts/wheel_smoke.sh
PYTHON=.venv/bin/python AER_SDIST_SMOKE_ROOT=/tmp/aer-sdist-smoke \
  scripts/sdist_smoke.sh
```

Coverage is measured for the `aer` package with subprocess coverage enabled and a configured line
threshold of 80%. `pytest --cov=aer --cov-report=term-missing` fails below that threshold; do not
replace it with file-existence or declaration checks.

The wheel and source-distribution smoke scripts create clean environments, execute the four
example builds outside the repository, reopen/patch/validate artifacts, and exercise Codex
installation without replacing an existing target. Do not claim render or visual validation when
the external tools are absent. When a required external tool is absent, verify the structured
`DEPENDENCY_MISSING` response instead.

## Contribution rules

Keep CLI functions thin and operation handlers directly callable. Register a capability only when
its implementation and compact schema agree. Test behavior rather than file existence: reopen
generated artifacts, compare unaffected semantic elements after patch, corrupt inputs, exercise
concurrency, and assert output budgets. Keep every changed line within the requested scope.

Commit messages use `type(scope): subject`, for example `feat(store): add locked content dedup`.
Generated artifacts, coverage, caches, virtual environments, and distribution output are ignored.

See the repository `AGENTS.md` for module boundaries, security invariants, and the final wheel
verification checklist.
