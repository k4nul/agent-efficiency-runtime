# Agent Efficiency Runtime

Agent Efficiency Runtime (AER) is a deterministic local execution layer that exposes only the
information an AI agent needs while keeping bulky data, generation code, logs, and artifacts out
of model context.

## Token waste it removes

AER replaces repeated Office/plotting boilerplate with versioned semantic specs, full-file reads
with bounded selectors, full regeneration with atomic patches, raw command logs with compact
diagnostics, and copied binary/data payloads with `aer://sha256/...` references. It also persists
task state and caller-supplied usage profiles locally. AER contains no LLM client and makes no
model API calls.

## Install

Linux and Python 3.11+ are supported. From a checkout:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install .
source .venv/bin/activate
export AER_HOME="$HOME/.aer"       # optional; this is already the default
aer --version
aer doctor
```

The default installation includes the Office, PDF, image, and chart libraries needed by the core
artifact workflows. v0.1 does not accept Parquet input. LibreOffice, Pandoc, and `pdftoppm` remain
optional external tools; a command that requires a missing tool returns `DEPENDENCY_MISSING` with
the dependency and affected capability.

## Five-minute quick start

Run these commands at the repository root:

```bash
mkdir -p example-output
aer discover "build and patch ppt"
aer schema presentation.build --compact --example
aer build examples/presentation.yaml --validate -o example-output/deck.pptx
aer inspect example-output/deck.pptx --selector "slide:id=metrics/shape:id=token-reduction-value"
aer patch example-output/deck.pptx --spec examples/patches/presentation.yaml --backup --validate
aer archive create example-output -o example-output.zip
aer archive verify example-output.zip
```

Every normal response is one compact JSON object. Use global `--pretty` or `--human` only when a
person needs a different presentation. Internal tracebacks appear only with global `--debug`.

## Command summary

| Command | Purpose |
|---|---|
| `aer discover`, `aer schema` | Find a capability locally, then disclose only its compact contract. |
| `aer store` | Deduplicate, verify, retrieve, pin, list, or garbage-collect content objects. |
| `aer inspect` | Read a bounded text/data/repository/Office/PDF selection. |
| `aer run -- ...` | Execute argv without a shell and return compact diagnostics plus a redacted log ref. |
| `aer data query` | Filter, project, sort, deduplicate, and aggregate local tabular data. |
| `aer build`, `aer patch`, `aer validate` | Create semantic artifacts, mutate selected elements, and verify them. |
| `aer convert`, `aer image`, `aer pdf`, `aer archive` | Deterministic conversion, media, PDF, and ZIP operations. |
| `aer state`, `aer recipe` | Persist long-task facts and run bounded trusted workflows. |
| `aer profile`, `aer benchmark` | Aggregate caller-supplied usage and run measured local comparisons. |
| `aer doctor` | Check core health and report optional capability availability. |

## Representative use

Long tests keep the sanitized full log locally:

```bash
aer run --timeout 300 -- pytest -q
aer store cat aer://sha256/REPLACE_WITH_RETURNED_DIGEST --start-line 10 --end-line 30
```

The runner spools output instead of retaining it in memory. It terminates a command that emits
more than 256 MiB across stdout and stderr, then stores the captured textual prefix after UTF-8
normalization, stdout/stderr sectioning, ANSI removal, and secret redaction. It reports
`output_limit_exceeded` and does not silently label that prefix as a full log.

Large data stays local; only up to 20 rows are previewed:

```bash
aer data query orders.xlsx --sheet Raw \
  --where "status == pending" --where "total >= 30000" \
  --select id,customer,total --sort total --descending --limit 100 \
  -o pending.csv
```

Task state survives conversation compaction:

```bash
aer state init release-01 --goal "Prepare the verified delivery"
aer state update release-01 --complete "PPTX built" --remaining "Human visual review"
aer state update release-01 --decision provider=TradingView --artifact deck=example-output/deck.pptx
aer state checkpoint release-01
```

## Codex integration

Install the wheel first, then install the included skill without changing existing Codex config:

```bash
./integrations/codex/install.sh --copy
```

The installer is included in the source checkout and source distribution. It detects
`$CODEX_HOME` (falling back to `~/.codex`), supports an explicit `--target`, and refuses to
overwrite a destination. Use `--symlink` only for development when the checkout will remain in
place. The project-level guidance is available at `integrations/codex/AGENTS-snippet.md`.

## Validation and benchmarks

```bash
aer validate example-output/deck.pptx
aer benchmark run --scenario log-compaction
aer benchmark report
aer profile compare --task ppt-generation
```

Benchmarks measure bytes, wall time, validity, and hashes from real local workloads. Their token
figures are explicitly estimates calculated as `ceil(UTF-8 bytes / 4)`; they are not
provider-billed token counts. Office render validation uses LibreOffice when installed. Direct PDF
render validation uses `pdftoppm` to rasterize at most the first three pages and stores the PNG
previews by reference. Neither check claims human-level visual approval.

## Security

AER uses safe YAML loading, argv subprocesses with `shell=False`, process-group timeouts, secret
redaction before log storage, atomic replacement, SHA-256 preconditions, archive traversal checks,
symlink restrictions, image/ZIP/data/output limits, and a capability-only recipe allowlist. Raw
recipe commands are disabled unless both trust and raw-command permission are explicit. AER never
fetches URLs, evaluates user code, executes macros, or runs embedded Office payloads. Semantic
specs are limited to 4 MiB and patch targets to 256 MiB. ZIP/OOXML expansion is limited to 10,000
entries, 512 MiB total, 256 MiB per entry, and a 200:1 ratio for entries of at least 1 MiB. Office
conversion and render validation reject external relationships, macros, and executable parts.
Regex operations combine structural rejection with execution timeouts. Formula-like CSV/TSV query
or conversion values are written to XLSX as quoted text rather than formulas.

## Actual limitations

Linux is the first tested platform; Windows paths and process cleanup are designed for but not yet
continuously verified here. Office round-tripping covers the documented semantic blocks, not every
native feature. Formula strings are preserved but not calculated. Office-to-PDF and render checks
need LibreOffice; markup conversion needs Pandoc; PDF raster previews need `pdftoppm`. Automated
checks do not replace human visual review. v0.1 does not query Parquet, fetch external URLs,
preserve macro-enabled formats, provide a GUI, or measure provider tokens automatically. Profile
values are caller-supplied and their measured-versus-estimated provenance is not verified or
classified by v0.1; use provider usage values when available and record provenance in `notes`.

See `docs/` for protocols, specs, security assumptions, development commands, and deeper limits.
