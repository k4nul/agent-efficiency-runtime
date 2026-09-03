# Contributing to Agent Efficiency Runtime

Thank you for considering a contribution to Agent Efficiency Runtime (AER).

AER is intentionally conservative: it executes local operations, reads potentially large files,
and creates or mutates artifacts. Correctness, bounded output, reversibility, and explicit security
boundaries take priority over adding commands quickly.

## Before opening a change

- Search existing issues before creating a duplicate.
- Use a GitHub issue to discuss substantial features, new top-level commands, format changes, or
  compatibility breaks before implementation.
- Do not open a public issue for a suspected vulnerability. Follow [SECURITY.md](SECURITY.md).
- Keep pull requests focused on one coherent change.

## Development setup

Python 3.11 or newer is required. Linux is the first officially tested platform.

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Run the complete local validation gate before submitting a pull request:

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
git diff --check
```

Some conversion and render checks need optional system tools such as LibreOffice, Pandoc, or
`pdftoppm`. State clearly in the pull request which optional checks were available.

## Engineering requirements

Contributions must preserve the runtime invariants documented in [AGENTS.md](AGENTS.md) and the
security boundaries in [docs/security.md](docs/security.md). In particular:

- keep default CLI output bounded and machine-readable;
- avoid `eval`, `exec`, `shell=True`, unsafe YAML loaders, implicit URL fetching, and unbounded
  archive, image, data, or log processing;
- use atomic writes and stale-content preconditions where an operation can replace user data;
- redact secrets before persistent log storage;
- add behavior-level tests for new capabilities and regression tests for bug fixes;
- document user-visible behavior, limits, and compatibility changes;
- do not commit credentials, private data, local absolute paths, generated runtime stores, or
  unlicensed binary assets.

A new capability should fit an existing command family unless a separately discussed public API
change justifies a new top-level command.

## Pull requests

A pull request should explain:

1. the problem and the chosen design;
2. compatibility or security implications;
3. the validation commands that passed;
4. any checks that could not be run and why.

Update `CHANGELOG.md` for user-visible changes. Documentation and examples must describe implemented
behavior rather than planned behavior.

## Licensing

By submitting a contribution, you agree that your contribution may be distributed under the
project's [MIT License](LICENSE). Third-party material must retain its original notices and be
recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) when applicable.
