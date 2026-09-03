# Security model

AER processes local, user-selected inputs. It is not a sandbox for hostile executable code and it
does not elevate permissions.

## Enforced controls

- No `eval`, `exec`, unsafe YAML loading, implicit URL fetch, or `shell=True`. YAML specs and
  recipes use the safe loader and are then limited to 100 alias references, depth 100, and 100,000
  logical nodes after alias expansion; cyclic aliases are rejected. Values must be JSON-compatible,
  mapping keys must be strings, and floating-point values must be finite.
- Commands are argv arrays, run without stdin, bounded by timeout, and terminated as a process
  group where the OS supports it. Captured stdout and stderr share a 256 MiB hard limit. On limit
  breach AER terminates the process tree, sanitizes and stores every byte captured up to that point,
  and reports `output_limit_exceeded` instead of silently truncating a successful run.
- API keys, tokens, authorization headers, passwords, private keys, cookies, AWS/GitHub/OpenAI
  credentials, and credential-bearing database URLs are redacted before preview and log storage.
- Object paths derive only from validated lowercase SHA-256 digests. Writes are atomic and locked;
  input and output symlinks are rejected where replacement could cross a boundary.
- ZIP entry names are checked for absolute paths, `..`, and backslash traversal. ZIP/OOXML input is
  limited to 10,000 entries, 512 MiB total expanded bytes, 256 MiB per entry, and a 200:1 ratio for
  entries of at least 1 MiB. Archive creation rejects source symlinks.
- Images are EXIF-oriented and bounded by pixel count. Semantic specs are limited to 4 MiB and
  patch targets to 256 MiB; text, data, stdin, logs, recipes, and result counts have separate
  bounds. Nested unbounded regex quantifiers are rejected. Patch regex execution has a one-second
  timeout, and inspect regex matching has a 20-millisecond timeout per line.
- PDF inputs are limited to 256 MiB each and merge inputs to 512 MiB in aggregate. PDF text parsing
  runs in a separate process with a 10-second wall timeout, a 1 MiB extracted-text/result limit,
  16 KiB per query-match line, and, on POSIX, 8 CPU seconds and a 512 MiB address-space limit.
  Validation checks page count and media boxes throughout, while text presence and automatic
  empty-page evidence are limited to the first 100 pages.
- Artifact construction is bounded before materialization: 10,000 rendered document elements,
  1,000 presentation slides with 10,000 list entries in aggregate across `content` and all nested
  lists, 100 workbook sheets, and 1,000,000 workbook or data-query cells. Workbook and
  tabular-conversion paths enforce a 100,000-row limit; the data-query engine permits up to
  1,000,000 rows subject to its cell limit.
- Patch expected hashes prevent stale writes. A failed multi-operation patch leaves the original
  unchanged.
- Recipes use an allowlist; raw command steps require explicit trust and permission. Recipes that
  contain `command.run` or a literal path in a content-bearing capability argument bypass the
  whole-recipe cache. Exact declared input and prior-step expressions remain eligible for
  content-hash-based caching.
- Values beginning with `=`, `+`, `-`, or `@` from CSV/TSV conversion and data-query output are
  stored as quoted text in XLSX, preventing them from becoming spreadsheet formulas.

## Office policy

AER never executes macros, OLE objects, or embedded executables. The supported build and patch
extensions are macro-free OOXML. Validation lists suspicious embedded parts and external
relationships. Office conversion and render validation reject documents containing an external
relationship, macro part, or executable part before invoking LibreOffice. v0.1 does not promise to
preserve macro-enabled files.

## Symlinks and deletion

Store inputs and internal paths reject symlinks. Archive sources reject symlink entries. Store GC
derives every deletion target from a validated digest and operates only under its configured
namespace; pinned objects are skipped and `--dry-run` is supported. AER has no command that deletes
an arbitrary user directory.

## Reversible compression exception

Ordinary overflow is stored in full. Command output is redacted first, so a secret discovered in a
log is intentionally not recoverable through its raw ref. A command stopped at the 256 MiB capture
limit has a recoverable redacted prefix, not output that the terminated process never produced.
PDF text is another explicit exception: if `extraction_truncated` is true, its `raw_ref` contains
only the bounded extracted prefix or match set and `raw_content_complete` is false.
