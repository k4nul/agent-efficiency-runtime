# Changelog

All notable changes to this project are documented here.

## 0.1.1 - 2026-09-01

- Aligns package metadata, installation guidance, CI, and clean-wheel verification after the
  initial v0.1.0 tag.
- Adds hard depth/count/byte limits for structured inspection, state, recipes, images, PDF
  attachment names, patch operation lists, and cache hashing inputs.
- Preserves PPTX text formatting during selected patches, supports formula-valued XLSX cell
  patches, validates formula specs, and validates staged builds before atomic publication.
- Bundles OFL-licensed NanumGothic for system-font-independent Korean chart glyph support and
  rejects silently ignored presentation theme/aspect-ratio values.
- Corrects per-success profile accounting and provenance labels, recipe cache invalidation and
  cache metrics, raw-log preservation semantics, and output/schema documentation.
- Hardens concurrent SQLite and file locking with rollback-journal mode, stable native lock-file
  identity, and fail-closed lock behavior.

## 0.1.0 - 2026-08-31

- Initial local runtime with compact JSON protocol, content store, discovery, bounded inspection,
  command compaction, local data queries, semantic Office/chart builders, atomic patches,
  validation, deterministic image/PDF/archive operations, task state, trusted recipes, profiling,
  measured benchmarks, diagnostics, and Codex integration.
- Security limits cover spec and patch size, ZIP expansion and compression ratio, image pixels,
  regex execution, and a 256 MiB combined command capture. Stored command logs are redacted.
- Render validation produces dependency-backed evidence only: LibreOffice converts Office files,
  and `pdftoppm` stores bounded PDF raster preview refs without asserting human visual quality.
- Office conversion/render rejects active content, and CSV/TSV values that resemble formulas are
  quoted as text when exported to XLSX.
