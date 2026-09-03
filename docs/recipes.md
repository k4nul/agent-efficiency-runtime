# Recipes

Recipes are bounded YAML workflows composed only of registered capabilities. Inputs are typed as
`string`, `path`, `integer`, or `boolean`. Expressions reference an input or a prior step result:

```yaml
version: 1
name: office-delivery
description: Build, validate, and package a DOCX delivery.
inputs:
  spec: {type: path}
  output_dir: {type: path}
steps:
  - id: build-docx
    uses: document.build
    with:
      spec: "${{ inputs.spec }}"
      output: "${{ inputs.output_dir }}/report.docx"
  - id: validate-docx
    uses: artifact.validate
    with: {target: "${{ steps.build-docx.output }}"}
```

Built-ins are `office-delivery`, `project-package`, `data-extract`, `test-and-package`, and
`presentation-delivery`. They are packaged into the wheel. Named recipes placed directly in
`$AER_HOME/recipes` are trusted by placement. A recipe loaded from any other path requires
`--trust`.

`command.run` steps additionally require `--allow-raw-command`, and that flag is honored only for a
trusted recipe. It still executes an argv array with `shell=False`. Python/eval/exec recipe steps do
not exist.

Recipe YAML is safe-loaded and bounded; excessive aliases, nesting, logical node expansion, and
cyclic aliases are rejected before execution. Mapping keys must be strings, values must use
JSON-compatible types, and floating-point values must be finite. The engine also rejects unknown
capabilities, missing or unknown capability arguments, invalid input expressions, forward step
references, dependency cycles, more than 50 steps, and expired overall timeouts. A step may
reference only a previously completed step.

The engine fails fast and stores a JSONL run log. A whole recipe is cacheable only when it has no
`command.run` step and every content-bearing path argument is an exact declared-input or prior-step
expression (including lists composed entirely of such expressions). Declared file and directory
inputs are content-hashed, and cached side-effect results are reused only while their output paths
exist with the recorded content hashes. A recipe with a literal content-bearing path bypasses both
recipe-cache reads and writes; literal destination paths alone do not disable caching.
