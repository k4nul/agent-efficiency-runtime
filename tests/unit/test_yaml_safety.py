from __future__ import annotations

import pytest

import aer.yaml_safety as yaml_safety
from aer.errors import AerError
from aer.yaml_safety import load_yaml_safely


def test_nested_alias_fanout_counts_logical_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yaml_safety, "MAX_YAML_NODES", 100)
    text = (
        "a: &a [x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a]\nc: &c [*b, *b, *b, *b, *b]\nvalue: *c\n"
    )

    with pytest.raises(AerError) as captured:
        load_yaml_safely(text, operation="test", target="fanout.yaml")

    assert captured.value.code == "LIMIT_EXCEEDED"
    assert captured.value.details == {"limit": 100}


@pytest.mark.parametrize("text", ["1: value\n", "value: !!set {a: null}\n", "value: .nan\n"])
def test_yaml_requires_deterministic_json_compatible_values(text: str) -> None:
    with pytest.raises(AerError) as captured:
        load_yaml_safely(text, operation="test", target="invalid.yaml")

    assert captured.value.code == "INVALID_SPEC"
