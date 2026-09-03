# Patch specification v1

Patch files are safe-loaded version 1 YAML/JSON with one or more operations. Every operation is
applied to an in-memory working copy. Patch specs are limited to 4 MiB and targets to 256 MiB. The
target is replaced only after all operations and optional validation succeed; a final content-hash
check detects a concurrent change before replacement.

```yaml
version: 1
operations:
  - op: pptx.set_text
    target: slide:id=metrics/shape:id=token-reduction-value
    value: "63%"
```

Use `--dry-run` for a change plan, `--backup` for `<target>.bak`, `--expected-sha256` for stale-file
protection, and `--validate` for post-patch structural verification.

## Operations

- Text: `text.replace`, `text.regex_replace`.
- JSON: `json.set`, `json.remove`, `json.insert` with RFC 6901 pointers.
- YAML: `yaml.set`, `yaml.remove`, `yaml.insert` with the same pointer rules.
- PPTX: `pptx.set_text`, `pptx.replace_text`, `pptx.remove_shape`,
  `pptx.update_chart_data`.
- DOCX: `docx.replace_text`, `docx.set_block`, `docx.remove_block`.
- XLSX: `xlsx.set_cell`, `xlsx.set_range`, `xlsx.replace_text`, `xlsx.clear_range`.

PPTX selectors use `slide:id=ID/shape:id=ID`. DOCX selectors use `block:id=ID`. XLSX accepts
`Sheet!A1`, `Sheet!A1:B3`, and stable `sheet:id=.../cell=A1` forms. JSON Pointer escapes `~` as
`~0` and `/` as `~1`.

Text replacement combines split OOXML runs before replacement. The replacement is placed in the
first affected run while unaffected run text and formatting are retained. Text regex patterns are
structurally screened and run with a one-second timeout. When the target cannot be safely edited,
AER fails the whole patch rather than reporting partial success. Macro-enabled Office formats are
not patch targets. Generated DOCX list blocks use one bookmark range across all of their
paragraphs: `docx.set_block` replaces that complete range with one paragraph, while
`docx.remove_block` removes every paragraph in the range. PPTX shape and DOCX block removals also
remove the corresponding sidecar manifest selector before optional validation.
