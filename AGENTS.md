# Agent Efficiency Runtime contributor guide

## Purpose

Agent Efficiency Runtime (`aer`) is a deterministic local execution layer. It keeps large
files, repetitive generation code, raw data, long logs, and task state outside model context,
then returns bounded JSON and reversible `aer://sha256/...` references. It never calls an LLM.

## Package map

- `protocol`, `config`, `paths`, `hashing`, `limits`: shared contracts and safety boundaries.
- `store`, `cache`: persistent and disposable content-addressed storage.
- `registry`, `inspect`, `runner`, `data`: discovery and compact local execution.
- `artifacts`, `patch`, `validation`, `conversion`: build, mutate, and verify artifacts.
- `image`, `pdf`, `archive`: deterministic media and delivery operations.
- `state`, `recipes`, `profile`, `benchmark`, `doctor`: long-running workflow support and
  measurement.
- `integrations/codex`: optional Codex skill and project instruction snippet.

Python 3.11 and newer are supported. Linux is the first officially tested platform.

## Setup and checks

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/pytest
.venv/bin/pytest --cov=aer --cov-report=term-missing
.venv/bin/python -m build
```

The coverage configuration enables subprocess measurement and enforces at least 80% package line
coverage for the coverage run.

Before completion, run every command above and install the built wheel into a separate clean
virtual environment. Run `aer --version`, `aer doctor`, discovery, compact schema lookup, and
the repository examples from that installed wheel.

## Runtime invariants

- Default CLI output is one compact JSON object. Do not add progress bars, ANSI, success-test
  lists, default tracebacks, or unbounded previews.
- Preserve full non-secret overflow in the object store and return an `aer://sha256/...` ref.
- Prefer a selector-based patch to full regeneration. Every patch must be atomic and support a
  stale SHA-256 precondition where applicable.
- Use content hashes, normalized specs, capability versions, configuration, and dependency
  versions for cache correctness. An mtime is not a cache key.
- Keep persistent store objects separate from disposable cache objects.

## Adding a capability

Implement a directly callable operation handler, add accurate metadata and a compact schema in
`aer.registry`, expose it through an existing top-level CLI family, and add behavior-level tests.
Do not add a new top-level command without changing the public command policy. Only advertise
the capability after reopen or execution tests pass.

## Security boundaries

Never use `eval`, `exec`, `shell=True`, unsafe YAML loaders, automatic URL fetching, or implicit
recipe commands. Reject traversal and unsafe symlinks, bound archive/image/data/log sizes, redact
stored command logs, use argv subprocesses with timeouts, and write outputs atomically. AER never
executes Office macros or embedded payloads. Raw recipe commands require explicit trust and an
explicit allow flag; arbitrary Python recipe steps remain unsupported. Preserve the implemented
hard limits: 4 MiB specs, 256 MiB patch targets, 256 MiB combined command output, and ZIP/OOXML
limits of 10,000 entries, 512 MiB total expansion, 256 MiB per entry, and 200:1 compression for
entries of at least 1 MiB.
