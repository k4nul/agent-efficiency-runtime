# Token measurement

`aer profile record` stores values supplied by the caller or model provider. It does not infer a
missing provider field. Unknown values remain `null`, aggregate completeness flags remain false,
and cached input tokens are treated as a subset rather than added twice.

Reports calculate reported total tokens, tokens/model calls/tool calls per successful task, retry
average, success rate, duration, and human edits. Comparisons use only variants with complete
required token fields and label provider-billed totals as unknown unless supplied externally.

`aer benchmark run` executes six local comparisons:

1. full 5,000-line log versus compact failure context;
2. full 10,000-row CSV context versus local query preview;
3. direct python-pptx source bytes versus semantic presentation spec bytes;
4. full JSON rewrite versus JSON Pointer patch;
5. whole presentation rebuild versus element patch request;
6. repeated package-script request versus recipe request.

Each variant records locally measured input/output/context bytes, wall time, validity, and SHA-256.
Those measurements are real for the benchmark workload; they are not model-provider telemetry.
Estimated tokens are calculated as `ceil(UTF-8 bytes / 4)` and always include:

```json
{
  "estimation_method": "ceil(utf8_bytes/4)",
  "not_provider_billed_tokens": true
}
```

This heuristic is useful for within-benchmark comparisons, not billing or model-specific token
accounting. It does not measure cached-token pricing, tokenizer-specific segmentation, or provider
overhead. No fixed token-reduction percentage is shipped as a result.
