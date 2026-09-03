# Limitations in v0.1

- Linux is the first officially validated platform. Windows-specific process-tree behavior is
  implemented conservatively but is not covered by this release environment.
- Office builders and patches cover the documented semantic layouts/blocks. They do not guarantee
  round-trip preservation of animations, advanced master layouts, SmartArt, tracked changes,
  comments, pivot tables, or every native chart feature.
- Macro-enabled Office formats are not supported patch targets and macro preservation is not
  promised. Embedded payloads are reported, never executed.
- Excel formulas must begin with `=` and receive only a basic nonempty/parenthesis-balance check;
  they are stored but not calculated.
- Office-to-PDF and Office render validation require LibreOffice. Markup conversion requires
  Pandoc. Direct PDF `--render` validation requires `pdftoppm`; Office render validation still
  reports PDF conversion evidence when `pdftoppm` is absent, but omits raster previews. Those tools
  are not bundled, and required-but-missing tools produce `DEPENDENCY_MISSING` rather than a fake
  result.
- Generated PDF operations manipulate existing PDFs; the artifact spec has no low-level PDF kind.
- Each PDF input is limited to 256 MiB; merge also limits aggregate input to 512 MiB. PDF v0.1
  operations accept at most 100 merge inputs and 10,000 source/output pages per operation.
  `pdf split` creates at most 1,000 files, and merge/extract/split stream no more than 512 MiB of
  aggregate output before returning `LIMIT_EXCEEDED`; callers should batch larger jobs.
- PDF text extraction runs in a separate bounded worker. A selected page retains at most 1 MiB of
  extracted UTF-8 text; query matches retain at most 1 MiB in aggregate and 16 KiB per matching
  line. The worker has a 10-second wall timeout and, on POSIX, 8 CPU seconds and a 512 MiB address
  space. PDF validation checks page count and media boxes across every accepted page, but text
  presence and automatic empty-page evidence only for the first 100 pages. When extraction is
  truncated, `raw_content_complete: false` explicitly means that `raw_ref` is a bounded prefix or
  match set, not the complete extracted text.
- Build-time materialization is bounded. Documents allow at most 10,000 rendered blocks, expanded
  list items, and table cells in aggregate. Presentations allow at most 1,000 slides and 10,000 list
  entries in aggregate across `content` and every nested list. Workbooks allow at most 100 sheets,
  100,000 rows per sheet, 1,000,000 materialized cells in aggregate, and 10,000 chart or
  conditional-format records.
- Automated layout, overlap, density, reopen, and render checks do not constitute human visual
  approval.
- Chart output uses the bundled NanumGothic font for system-font-independent Korean glyph support;
  exact raster or SVG bytes can still vary with Matplotlib, FreeType, and Pillow versions. PPTX,
  DOCX, and XLSX outputs request Korean-capable font names but do not embed fonts; viewer fallback
  and render fidelity depend on fonts installed on the viewing system.
- CSV/JSON/XLSX queries currently load bounded data into memory. Query input is limited to 256 MiB
  and 1,000,000 rows, with at most 1,000,000 row-by-column cells. Chart and tabular-conversion paths
  use tighter 64 MiB and 100,000-row limits; tabular conversion also enforces the 1,000,000-cell
  limit. Joins and Parquet input are not implemented in v0.1.
- Regex support is deliberately restricted. Structural checks and runtime timeouts reduce denial
  of service risk, but AER is not a general untrusted-regex execution service.
- The command runner terminates a process after more than 256 MiB combined stdout/stderr. Its raw
  ref then preserves the redacted captured prefix, not output the command might have emitted later.
- Deterministic ZIP output is byte-identical for the same files in the same Python/zlib
  implementation. Cross-implementation compression bytes are not promised.
- Semantic Office builds normalize timestamps and IDs where practical but do not promise
  byte-identical OOXML packages.
- `business-clean` is the only built-in presentation theme in v0.1. Its packaged JSON is theme
  metadata, not a custom-template interface. The optional `aer doctor` template check reports
  whether that file is packaged and explicitly reports `custom_templates_supported: false`.
- AER does not provide an LLM, web service, accounts, browser automation, GUI, HWPX, external URL
  fetch, generative image expansion, or arbitrary code sandbox.
- Profile values are caller-supplied. v0.1 cannot verify their provenance or classify them as
  measured versus estimated, and it cannot discover provider billing usage automatically.
  Benchmark token values are explicitly byte-derived estimates.
