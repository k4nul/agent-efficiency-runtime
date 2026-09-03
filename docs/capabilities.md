# Capabilities

Use `aer discover "intent"` and then `aer schema NAME --compact`; do not preload this catalog into
an agent prompt. `aer schema --list-names` is the authoritative installed list.

| Family | Implemented v0.1 behavior |
|---|---|
| Store | SHA-256 put/get/cat/stat/verify/list/pin/GC, locked dedup, corruption checks |
| Inspect | bounded text, JSON, safe YAML, CSV/TSV/JSONL, Git repositories, XLSX, PPTX, DOCX, PDF selections with reversible text compaction |
| Runner | shell-free argv, timeout/tree cleanup, redacted log store, 256 MiB capture hard limit, bounded failure context |
| Data | CSV/TSV/JSON/JSONL/XLSX filter/select/rename/sort/page/unique/duplicate/aggregate |
| Build | presentation, document, workbook, chart PNG/SVG, HTML, Markdown semantic specs |
| Patch | text/regex, JSON/YAML pointer, PPTX, DOCX, XLSX selected mutations |
| Validate | OOXML/ZIP/package reopen and format checks; Office render and up-to-three-page PDF raster evidence when dependencies exist |
| Convert | CSV/TSV↔XLSX with formula-like input quoted as text, deterministic image formats; conditional Office/markup conversion |
| Image | inspect, resize, crop, cover/contain fit, batch manifest |
| PDF | inspect, merge, page extraction, split |
| Archive | deterministic ZIP, embedded hash manifest, verify/list |
| State/recipe | atomic state files, checkpoints, typed allowlisted workflows, whole-recipe cache/trust policy |
| Profile/benchmark | caller-supplied usage aggregation and six executed local comparisons |
| Doctor | core health, packaged `business-clean` theme metadata, built-in recipes, and capability-specific optional dependency status |

Lexical discovery uses capability names, summaries, and keywords with deterministic fuzzy fallback.
It never sends the query off the machine.

Semantic specs are accepted up to 4 MiB and patch targets up to 256 MiB. ZIP/OOXML readers enforce
10,000 entries, 512 MiB total expansion, 256 MiB per entry, and a 200:1 compression-ratio ceiling
for entries of at least 1 MiB. Office conversion/render rejects external relationships and active
parts. Direct PDF render preview requires `pdftoppm`; Office render requires LibreOffice and uses
`pdftoppm` for preview refs only when it is also available.

Builds reject more than 10,000 rendered document elements, more than 1,000 presentation slides or
10,000 list entries in aggregate across presentation `content` and all nested lists, and more than
100 workbook sheets or 1,000,000 materialized workbook cells. Data queries enforce 1,000,000-row
and 1,000,000-cell bounds; workbooks, chart inputs, and tabular conversions use the tighter
100,000-row bound. PDF inputs are limited to 256 MiB each and merge inputs to 512 MiB in aggregate.
PDF operations accept at most 100 merge inputs and 10,000 pages, `pdf split` creates at most 1,000
files, and constructed PDF output is bounded to 512 MiB per operation.

Default text, repository, PPTX, DOCX, and PDF previews mark shortened fields with
`text_truncated`. The CLI stores the exact selected or matched records under `raw_ref`; `--full`
returns exact selected text when it remains within the command's hard input/output bounds. PDF text
uses an isolated worker with a 1 MiB extracted-text/result limit and 10-second timeout. When that
worker truncates a page prefix or query match set, `extraction_truncated` and
`raw_content_complete: false` distinguish the bounded `raw_ref` from a complete extraction. PDF
validation checks page count and media boxes throughout; only text presence and automatic
empty-page evidence are limited to the first 100 pages.
`aer doctor` has an optional check for the packaged metadata file for the one built-in presentation
theme, `business-clean`; it reports `custom_templates_supported: false` and does not advertise a
custom template system.

Unsupported in v0.1: Parquet query, joins, external URL fetch, HWPX, macro-enabled Office
round-trip, browser automation, a spreadsheet calculation engine, arbitrary recipe code, and GUI
editing.

`schemas/artifact-v1.schema.json` is a source-distribution reference envelope. Executable
block-level validation is provided by `aer build SPEC --dry-run`; standalone inspect, build, data,
conversion, and render results are not cached in v0.1.
