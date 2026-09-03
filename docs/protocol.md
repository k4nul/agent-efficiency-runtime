# Compact response protocol

The default stdout value is exactly one UTF-8 JSON object. ANSI and progress bars are disabled.
`--pretty` adds indentation, `--human` returns a short human line, and `--debug` is the only mode
that prints an internal traceback to stderr.

Success shape:

```json
{
  "ok": true,
  "operation": "presentation.build",
  "result": {},
  "artifacts": [],
  "warnings": [],
  "metrics": {
    "duration_ms": 0,
    "bytes_read": 0,
    "bytes_written": 0,
    "cache_hit": false
  }
}
```

Failure shape:

```json
{
  "ok": false,
  "operation": "presentation.patch",
  "code": "HASH_MISMATCH",
  "message": "Target changed since the expected hash was recorded.",
  "target": "deck.pptx",
  "details": {},
  "suggested_action": "Inspect the current file and regenerate the patch.",
  "raw_ref": null
}
```

## Exit codes

| Exit | Codes |
|---:|---|
| 0 | success |
| 2 | `INVALID_ARGUMENT`, `INVALID_SPEC`, `INVALID_SELECTOR`, `INVALID_PATCH` |
| 3 | `NOT_FOUND` |
| 4 | `UNSUPPORTED_FORMAT` |
| 5 | `DEPENDENCY_MISSING` |
| 6 | `CONFLICT`, `HASH_MISMATCH` |
| 7 | `LIMIT_EXCEEDED` |
| 8 | `COMMAND_FAILED` |
| 9 | `COMMAND_TIMEOUT` |
| 10 | `CORRUPT_FILE` |
| 11 | `VALIDATION_FAILED`, `TEXT_OVERFLOW` |
| 12 | `PATH_OUTSIDE_ROOT` |
| 13 | `UNTRUSTED_RECIPE` |
| 70 | `INTERNAL_ERROR` |

## Budgets and reversibility

Discovery is tested below 2 KiB, compact schema below 4 KiB, and normal command/inspect/run output
below 16 KiB. Tabular previews contain at most 20 rows. Log previews contain at most 80 lines or
16 KiB. When a full text or structured result exceeds its budget, the sanitized/full source is
stored and `raw_ref` or `result_ref` identifies it. Binary store content is never emitted by
`store cat`.

Secret safety takes precedence over byte-for-byte reversibility: stored command logs are a
UTF-8-normalized, stdout/stderr-sectioned textual capture after secret redaction and terminal ANSI
removal, not an unredacted binary archive. Progress lines, carriage returns, blank lines, and
trailing whitespace remain in the stored ref even though compact diagnostics omit them. If
combined stdout/stderr exceeds 256 MiB, AER terminates the command and the ref contains the
normalized, redacted captured prefix; `output_limit_exceeded` distinguishes it from a complete
successful log.
Resource safety also bounds PDF text extraction. A selected page or query returns at most 1 MiB of
extracted text or match records; if that boundary is reached, `extraction_truncated` is true and
`raw_content_complete: false` states that the ref contains only the captured prefix or match set.
