from __future__ import annotations

import json

import pytest

from aer.errors import AerError
from aer.registry import discover, list_names, schema


def test_discovery_is_deterministic_and_relevant() -> None:
    first = discover("edit ppt title", limit=3)
    second = discover("edit ppt title", limit=3)

    assert first == second
    capabilities = first["capabilities"]
    assert isinstance(capabilities, list)
    assert capabilities[0]["name"] == "presentation.patch"
    assert set(capabilities[0]) == {"name", "summary"}


def test_discovery_handles_fuzzy_query_and_pdf_merge() -> None:
    fuzzy = discover("presntation ptch", limit=1)
    merge = discover("merge pdf", limit=2)

    assert fuzzy["capabilities"][0]["name"] == "presentation.patch"
    assert merge["capabilities"][0]["name"] == "pdf.merge"


def test_compact_schema_discloses_only_selected_fields_under_budget() -> None:
    record = schema("presentation.patch", compact=True, example=True)
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()

    assert record["required"].keys() == {"target", "spec"}
    assert "pptx.set_text" in record["operations"]
    assert record["guidance"]["operation_fields"]["pptx.set_text"] == "target, value"
    assert record["example"]["patch"]["version"] == 1
    assert "input_schema" not in record
    assert len(encoded) < 4096


def test_full_schema_and_names_are_stable() -> None:
    names = list_names()["names"]
    record = schema("data.query")

    assert names == sorted(names)
    assert len(names) == len(set(names))
    assert {"artifact.inspect", "command.run", "data.query", "runtime.doctor"} <= set(names)
    assert record["input_schema"]["additionalProperties"] is False


def test_catalog_matches_executable_cli_contract_and_compact_budgets() -> None:
    names = list_names()["names"]
    for name in names:
        encoded = json.dumps(
            schema(name, compact=True), ensure_ascii=False, separators=(",", ":")
        ).encode()
        assert len(encoded) < 4096, name

    assert {"pin", "unpin"} <= set(schema("store.content", compact=True)["operations"])
    assert {"regex", "sheet", "range", "slide", "page", "changed"} <= set(
        schema("artifact.inspect", compact=True)["optional"]
    )
    assert "Parquet" not in schema("data.query", compact=True)["required"]["source"]["description"]
    assert {"x", "y", "ratio", "mode", "strip_metadata"} <= set(
        schema("image.transform", compact=True)["optional"]
    )
    assert {"trust", "allow_raw_command", "timeout"} <= set(
        schema("recipe.run", compact=True)["optional"]
    )
    assert {"cached_input_tokens", "tool_schema_tokens", "human_edits"} <= set(
        schema("profile.record", compact=True)["optional"]
    )
    assert "horizontal-bar" in schema("chart.build", compact=True)["operations"]
    validation = schema("artifact.validate", compact=True)
    assert "human visual review" in validation["guidance"]["render"]
    assert "LibreOffice" in validation["requires"][0]
    chart_example = schema("chart.build", compact=True, example=True)["example"]
    assert chart_example["spec"]["source"] == "data.csv"
    assert chart_example["data_csv"].startswith("workflow,tokens")
    markup = schema("markup.build", compact=True, example=True)
    assert markup["guidance"]["spec_required"][1] == "kind=html|markdown"
    assert markup["example"]["spec"]["kind"] == "html"
    for name in (
        "presentation.build",
        "document.build",
        "workbook.build",
        "chart.build",
        "markup.build",
    ):
        record = schema(name, compact=True, example=True)
        assert record["required"].keys() == {"spec"}
        assert "required unless dry_run" in record["optional"]["output"]["description"]
        if name in {"presentation.build", "document.build", "workbook.build"}:
            assert record["guidance"]["spec_required"][0] == "version=1"
            assert record["example"]["spec"]["version"] == 1


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (lambda: discover(""), "INVALID_ARGUMENT"),
        (lambda: discover("x" * 4097), "LIMIT_EXCEEDED"),
        (lambda: discover("pdf", limit=0), "INVALID_ARGUMENT"),
        (lambda: schema("missing.capability"), "NOT_FOUND"),
    ],
)
def test_registry_returns_stable_errors(call: object, code: str) -> None:
    with pytest.raises(AerError) as captured:
        call()  # type: ignore[operator]
    assert captured.value.code == code
