## Summary

Describe the problem and the implemented change.

## Design and compatibility

Explain important design choices, public API or output changes, migration needs, and security
implications.

## Validation

List the exact commands that passed. State which optional dependency-backed checks were unavailable.

```text
ruff check .
ruff format --check .
mypy src
pytest
pytest --cov=aer --cov-report=term-missing
python -m build
wheel smoke:
sdist smoke:
```

## Checklist

- [ ] The change is focused and contains no credentials, private data, or local absolute paths.
- [ ] Tests cover new behavior or the reported regression.
- [ ] Default output remains bounded and machine-readable.
- [ ] File replacement is atomic and reversible where applicable.
- [ ] Security limits and secret redaction remain enforced.
- [ ] User-visible behavior and limitations are documented.
- [ ] `CHANGELOG.md` is updated when the change affects users.
- [ ] Third-party code or assets include compatible licensing and notices.
