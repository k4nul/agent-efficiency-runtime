# Changelog

All notable changes to this project are documented here.

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
