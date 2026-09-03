# Agent Efficiency Runtime

[한국어](README.ko.md)

[![CI](https://github.com/k4nul/agent-efficiency-runtime/actions/workflows/ci.yml/badge.svg)](https://github.com/k4nul/agent-efficiency-runtime/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Agent Efficiency Runtime (AER) is a deterministic local execution layer that exposes only the
information an AI agent needs while keeping bulky data, generation code, logs, and artifacts out
of model context.

AER does not contain an LLM client and does not call a model API. It is a local tool runtime that
an agent, script, or human can invoke through a bounded JSON-oriented CLI.

## Why AER exists

Agent workflows often waste context and execution effort by repeatedly:

- generating Office, PDF, image, and chart boilerplate;
- reading complete files when only one section, range, sheet, slide, or field is needed;
- regenerating an entire artifact to change a small element;
- copying long command logs and binary payloads into model context;
- reconstructing task state after conversation compaction.

AER replaces those patterns with versioned semantic specifications, bounded selectors, atomic
patches, compact diagnostics, persistent task state, and content-addressed
`aer://sha256/...` references.

## Capabilities

| Command family | Purpose |
|---|---|
| `aer discover`, `aer schema` | Find a local capability and disclose only its compact contract. |
| `aer store` | Deduplicate, verify, retrieve, pin, list, or garbage-collect content objects. |
| `aer inspect` | Read a bounded text, data, repository, Office, or PDF selection. |
| `aer run -- ...` | Execute argv without a shell and return compact diagnostics plus a redacted log reference. |
| `aer data query` | Filter, project, sort, deduplicate, and aggregate local tabular data. |
| `aer build`, `aer patch`, `aer validate` | Create semantic artifacts, mutate selected elements, and verify the result. |
| `aer convert`, `aer image`, `aer pdf`, `aer archive` | Perform deterministic conversion, media, PDF, and delivery operations. |
| `aer state`, `aer recipe` | Persist long-task facts and run bounded trusted workflows. |
| `aer profile`, `aer benchmark` | Aggregate caller-supplied usage and run measured local comparisons. |
| `aer doctor` | Check core health and optional capability availability. |

The core artifact workflows cover DOCX, PPTX, XLSX, charts, images, PDFs, archives, and structured
or tabular local data.

## Installation

Linux is the first officially tested platform. Python 3.11 or newer is required.

```bash
git clone https://github.com/k4nul/agent-efficiency-runtime.git
cd agent-efficiency-runtime
python3.11 -m venv .venv
.venv/bin/python -m pip install .
source .venv/bin/activate
export AER_HOME="$HOME/.aer"  # optional; this is already the default
aer --version
aer doctor
```

The default Python installation includes the libraries needed by the core Office, PDF, image, and
chart workflows. LibreOffice, Pandoc, and `pdftoppm` are optional external tools. A command that
needs an unavailable tool returns `DEPENDENCY_MISSING` with the dependency and affected capability.

## Five-minute quick start

Run these commands from the repository root:

```bash
mkdir -p example-output
aer discover "build and patch ppt"
aer schema presentation.build --compact --example
aer build examples/presentation.yaml --validate -o example-output/deck.pptx
aer inspect example-output/deck.pptx \
  --selector "slide:id=metrics/shape:id=token-reduction-value"
aer patch example-output/deck.pptx \
  --spec examples/patches/presentation.yaml --backup --validate
aer archive create example-output -o example-output.zip
aer archive verify example-output.zip
```

Every normal response is one compact JSON object. Use global `--pretty` or `--human` only when a
person needs a different presentation. Internal tracebacks appear only with global `--debug`.

### Bounded command output

```bash
aer run --timeout 300 -- pytest -q
aer store cat aer://sha256/REPLACE_WITH_RETURNED_DIGEST \
  --start-line 10 --end-line 30
```

The runner spools output instead of retaining it in memory. If combined stdout and stderr exceed
256 MiB, it terminates the process and stores only the captured textual prefix after UTF-8
normalization, stdout/stderr sectioning, ANSI removal, and secret redaction. The response reports
`output_limit_exceeded` and does not label the prefix as a complete log.

### Local data query

Large data stays local and previews are limited to 20 rows by default:

```bash
aer data query orders.xlsx --sheet Raw \
  --where "status == pending" --where "total >= 30000" \
  --select id,customer,total --sort total --descending --limit 100 \
  -o pending.csv
```

### Persistent task state

```bash
aer state init release-01 --goal "Prepare the verified delivery"
aer state update release-01 \
  --complete "PPTX built" --remaining "Human visual review"
aer state update release-01 \
  --decision provider=TradingView --artifact deck=example-output/deck.pptx
aer state checkpoint release-01
```

## Codex integration

Install the wheel first, then install the included skill without replacing existing Codex
configuration:

```bash
./integrations/codex/install.sh --copy
```

The installer detects `$CODEX_HOME`, falls back to `~/.codex`, supports an explicit `--target`, and
refuses to overwrite an existing destination. Use `--symlink` only for development when the
checkout will remain available. Project-level guidance is provided in
`integrations/codex/AGENTS-snippet.md`.

## Validation and benchmarks

```bash
aer validate example-output/deck.pptx
aer benchmark run --scenario log-compaction
aer benchmark report
aer profile compare --task ppt-generation
```

Benchmarks measure bytes, wall time, validity, and hashes from real local workloads. Token figures
are estimates calculated as `ceil(UTF-8 bytes / 4)` and are not provider-billed token counts.
Office render validation uses LibreOffice when available. Direct PDF render validation uses
`pdftoppm` to rasterize at most the first three pages and stores PNG previews by reference. Neither
check claims human-level visual approval.

## Security model

AER uses safe YAML loading, argv subprocesses with `shell=False`, process-group timeouts, secret
redaction before log storage, atomic replacement, SHA-256 preconditions, archive traversal checks,
symlink restrictions, bounded regular-expression execution, and explicit image, ZIP, data, spec,
patch, and output limits.

Raw recipe commands are disabled unless both trust and raw-command permission are explicit. AER
does not fetch URLs, evaluate user code, execute macros, or run embedded Office payloads. Office
conversion and render validation reject external relationships, macros, and executable parts.
Formula-like CSV or TSV values are written to XLSX as quoted text rather than formulas.

See [docs/security.md](docs/security.md) for the threat model and [SECURITY.md](SECURITY.md) for
private vulnerability reporting.

## Current limitations

- Linux is the first continuously tested platform.
- Office round-tripping covers documented semantic blocks, not every native application feature.
- Formula strings are preserved but not calculated.
- Office-to-PDF and Office render checks require LibreOffice.
- Markup conversion requires Pandoc.
- PDF raster previews require `pdftoppm`.
- Automated validation does not replace human visual review.
- v0.1 does not query Parquet, fetch external URLs, preserve macro-enabled formats, provide a GUI,
  or measure provider tokens automatically.
- Profile values are caller-supplied; v0.1 does not verify whether a value is measured or estimated.

See [docs/](docs/) for protocols, specifications, development guidance, and detailed limits.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and
[AGENTS.md](AGENTS.md) before submitting a substantial change. Security vulnerabilities must not be
reported through a public issue.

## License

AER source code is distributed under the [MIT License](LICENSE). Bundled third-party assets retain
their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
