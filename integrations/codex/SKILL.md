---
name: agent-efficiency-runtime
description: Use the local `aer` CLI for Office, PDF, chart, image, archive, large-data, long-log, partial-patch, and durable task-state work when bounded model context and deterministic local execution matter. Do not use it for unsupported operations or as an LLM.
---

# Agent Efficiency Runtime

Use the installed `aer` command as the entrypoint; do not recreate its operations as one-off
Python unless discovery confirms the needed operation is unsupported.

1. Before PPTX, DOCX, XLSX, PDF, chart, image, archive, large-data, or long-log work, run a narrow
   `aer discover "<intent>"` query.
2. Read only `aer schema <capability> --compact`; request `--example` only when necessary.
3. Execute verbose commands through `aer run -- <argv...>`.
4. Inspect files with summaries, selectors, queries, ranges, or page/slide/sheet options instead
   of reading whole files.
5. For a small change, use `aer patch` with an expected SHA-256 instead of rebuilding.
6. Pass large results as `aer://sha256/...` refs; retrieve only required lines or selections.
7. Write one-off Python only after AER returns a specific unsupported capability or format.
8. Promote repeated deterministic work to a trusted recipe or a tested capability.
9. Preserve exact IDs, paths, formulas, numeric values, error names, dates, and hashes in summaries.
10. Run `aer validate` on deliverables. Render validation is automatic evidence, not human visual
    approval.

Run `aer doctor` when a capability reports `DEPENDENCY_MISSING` or installation health is unclear.
Never enable a raw-command recipe step without the user's authorization for that command.

