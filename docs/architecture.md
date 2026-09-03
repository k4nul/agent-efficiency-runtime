# Architecture

AER is a local, synchronous Python CLI. The command layer parses typed arguments and emits the
shared protocol; operation modules contain the behavior and can be invoked by trusted recipes
without recursively spawning the CLI.

```text
agent -> aer CLI -> operation handler -> local file/process/library
                    |       |       |
                    |       |       +-> atomic artifact output
                    |       +-> disposable whole-recipe hash cache
                    +-> persistent SHA-256 object store -> aer:// reference
```

## Boundaries

- `registry` provides cheap lexical/fuzzy discovery and per-capability schemas. It has no model or
  embedding dependency.
- `inspect`, `runner`, and `data` reduce information before it reaches stdout. Overflow goes to the
  persistent store; when an individual inspect text field is shortened, `text_truncated` marks the
  preview and the exact selected records remain available through `raw_ref` or explicit `--full`.
  Bounded PDF extraction is the explicit exception: `raw_content_complete: false` marks an
  extraction prefix or match set that reached the isolated worker's safety limit.
- `artifacts` creates PPTX, DOCX, XLSX, PNG/SVG, HTML, and Markdown from versioned semantic specs.
- `patch` loads and validates every operation in memory, then performs one atomic replacement.
- `validation` separates machine evidence from required human visual review.
- `recipes` dispatches an allowlisted operation handler directly. Arbitrary Python is never a step.
- `profile` records caller-supplied usage; `benchmark` executes local synthetic workloads.

## Local state

`AER_HOME` defaults to `~/.aer` and contains `config.toml`, `store`, `cache`, `state`, `recipes`,
`profiles`, and `database.sqlite3`. Blob bytes live in sharded SHA-256 paths. SQLite stores metadata,
cache mappings, profiles, and benchmark runs. Rollback-journal mode plus database-initialization and
per-object/file locks provide process-safe local access without memory-mapped WAL dependencies.
Persistent store objects and disposable cache objects use separate namespaces and roots.

## Stable artifact identity

- PPTX slide IDs are stored in `p:cSld/@name` as `aer:<id>`; shape names use
  `aer:<slide-id>/<shape-id>`.
- DOCX paragraph IDs use Word bookmarks named `aer_<id>`; tables use a table caption property.
- XLSX sheet names and cell coordinates are stable native selectors; explicit cell IDs become
  defined names.

Each build writes `<artifact>.aer.json` with the spec hash, artifact hash, and selector map. A patch
updates the artifact hash in an existing manifest.

## Atomicity and cache correctness

Files are serialized to memory or a private temporary file, fsynced, then replaced with
`os.replace`. Store publication uses a digest lock and never derives paths from untrusted names.
In v0.1, operational cache reuse is implemented for whole-recipe results; the cache module is not
yet wired into standalone inspect, build, data, conversion, or render commands. Recipe cache keys
include AER/dependency versions, content hashes, normalized recipe spec, and relevant configuration.
A changed mtime alone does not invalidate content identity. Recipe results are reused only when
recorded output hashes still match. Any recipe containing a
`command.run` step or a literal content-bearing path bypasses recipe-result caching entirely. Exact
declared-input and prior-step expressions remain cacheable, with declared file and directory inputs
represented by content hashes.
