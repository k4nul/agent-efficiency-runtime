# Artifact specification v1

Every spec is safe-loaded YAML or JSON with `version: 1`, a supported `kind`, optional `metadata`,
and semantic content. Spec files are limited to 4 MiB. `schemas/artifact-v1.schema.json` is an
envelope/reference schema, not a complete block-level validator; `aer build SPEC --dry-run` is the
authoritative executable validation.

## Presentation

```yaml
version: 1
kind: presentation
theme: business-clean
metadata: {title: AI Agent Token Optimization, locale: ko-KR}
footer: Agent Efficiency Runtime
content:
  - id: cover
    layout: title
    title: AI Agent Token Optimization
    subtitle: Deterministic local runtime
  - id: metrics
    layout: metrics
    title: 목표
    metrics:
      - {id: token-reduction, value: "50%+", label: 반복 작업 토큰 감소}
```

Layouts are `title`, `section`, `bullets`, `two-column`, `comparison`, `metrics`, `table`, `image`,
`image-with-caption`, `chart`, `quote`, `timeline`, and `closing`. Layouts choose safe coordinates;
the `bullets` layout may override `position` with inch values. The builder adds stable IDs, footer,
slide number, safe-fit images, and density checks. Korean Unicode is preserved and the Office
artifact requests `Noto Sans CJK KR`, but PPTX fonts are not embedded; viewer fallback and render
fidelity depend on fonts installed on the viewing system.
Version 1 supports the `business-clean` theme and `metadata.ratio: "16:9"`; other values are
rejected instead of being silently ignored.

## Document

Document content blocks are `title`, `heading`, `paragraph`, `bullets`, `numbered-list`, `table`,
`image`, `caption`, `quote`, `page-break`, `section-break`, `callout`, and `source-list`. Page size,
margins, header/footer, page numbers, locale metadata, table style, image width, and stable IDs are
supported. Generated DOCX is reopened during validation.

## Workbook

`sheets` contain an `id`, `name`, `columns`, rows (arrays or typed objects), explicit `cells`,
freeze pane, filter, widths, table, conditional formats, charts, and named ranges. A cell may use
`formula` and `number_format`; a formula must be a string beginning with `=`. AER preserves formula
strings, performs only a basic nonempty/parenthesis-balance validation, and requests recalculation
on open; it does not claim to calculate results.

## Chart

```yaml
version: 1
kind: chart
type: bar
source: data.csv
x: workflow
y: tokens
title: 작업 방식별 토큰 사용량
output: {width: 1400, height: 900}
```

Types are `bar`, `horizontal-bar`, `line`, `area`, `pie`, and `scatter`. PNG and SVG output are
supported. AER bundles and explicitly loads NanumGothic when rendering charts so Korean labels do
not depend on system fonts. Sources are local CSV, TSV, or JSON arrays; URLs are rejected by
omission.

HTML and Markdown accept string content or small semantic block lists. Use `aer build SPEC
--dry-run` to validate the plan without writing output.
