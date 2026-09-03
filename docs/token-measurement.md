# Token measurement

`aer profile record` stores caller-supplied values. Use model-provider usage fields when available,
but AER does not verify their source or classify a value as measured versus estimated in v0.1;
record that provenance in `notes`. Unknown values remain `null`, and completeness flags describe
field presence only. The accounting convention treats cached input, tool schema, and tool result
tokens as input subsets and reasoning tokens as an output subset. AER does not validate those
subset relationships. Provider-style total tokens are therefore `input_tokens + output_tokens`
without adding the diagnostic components again.

Reports calculate reported total tokens, tokens/model calls/tool calls per successful task, retry
average, success rate, duration, and human edits. Per-success cost divides the cost of every
recorded attempt, including failed attempts, by the number of successful tasks. Comparisons use
only variants whose input/output token fields are complete for every recorded attempt. v0.1 always
labels provider-billed totals as unknown; it has no billed-cost input or provider integration.

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
