## Agent Efficiency Runtime

For Office/PDF/chart/image/archive work, large local data, long logs, and partial file edits, use
`aer discover` followed by only the selected capability's compact schema. Run verbose processes
with `aer run --`, inspect selectors instead of whole files, patch rather than regenerate, and
pass large results as `aer://sha256/...` references. Preserve exact identifiers and values. Use
one-off Python only for an explicitly unsupported operation, and validate every deliverable with
`aer validate`.

