from __future__ import annotations

import csv
from pathlib import Path

import pytest

from aer.config import Settings
from aer.errors import AerError
from aer.profile import PROFILE_FIELDS, ProfileStore


def settings_for(home: Path) -> Settings:
    return Settings(
        home=home,
        store_dir=home / "store",
        cache_dir=home / "cache",
        state_dir=home / "state",
        recipes_dir=home / "recipes",
        profiles_dir=home / "profiles",
        database=home / "database.sqlite3",
        config_file=home / "config.toml",
    )


def test_profile_records_exact_fields_and_persists(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "aer")
    profiles = ProfileStore(settings)
    record = profiles.record(
        task="ppt-generation",
        variant="direct-python",
        timestamp="2026-08-31T01:02:03+00:00",
        model="provider-model",
        model_calls=8,
        tool_calls=14,
        input_tokens=48_210,
        cached_input_tokens=33_120,
        output_tokens=8_240,
        reasoning_tokens=2_000,
        tool_schema_tokens=700,
        tool_result_tokens=21_130,
        retries=3,
        duration_ms=12_345,
        success=True,
        human_edits=2,
        notes="measured from provider response",
    )

    assert record.reported_total_tokens == 80_280
    assert record.missing_token_fields == ()
    assert record.as_dict()["provider_billed_tokens"] is None
    assert record.as_dict()["measurement_source"] == "user_recorded"
    assert ProfileStore(settings).list()[0] == record
    with settings.connect() as connection:
        columns = [
            str(row["name"]) for row in connection.execute("PRAGMA table_info(aer_profiles)")
        ]
    assert tuple(columns) == PROFILE_FIELDS


def test_report_and_compare_use_actual_recorded_aggregates(tmp_path: Path) -> None:
    profiles = ProfileStore(settings_for(tmp_path / "aer"))
    complete = {
        "reasoning_tokens": 5,
        "tool_schema_tokens": 2,
        "tool_result_tokens": 10,
    }
    profiles.record(
        task="ppt",
        variant="direct",
        success=True,
        model_calls=8,
        tool_calls=14,
        input_tokens=100,
        output_tokens=20,
        cached_input_tokens=90,
        retries=3,
        duration_ms=1_000,
        human_edits=2,
        **complete,
    )
    profiles.record(
        task="ppt",
        variant="direct",
        success=False,
        model_calls=2,
        tool_calls=4,
        input_tokens=40,
        output_tokens=10,
        cached_input_tokens=30,
        retries=1,
        duration_ms=500,
        human_edits=0,
        **complete,
    )
    profiles.record(
        task="ppt",
        variant="aer",
        success=True,
        model_calls=3,
        tool_calls=5,
        input_tokens=40,
        output_tokens=10,
        cached_input_tokens=35,
        retries=0,
        duration_ms=400,
        human_edits=0,
        reasoning_tokens=0,
        tool_schema_tokens=2,
        tool_result_tokens=8,
    )

    report = profiles.report(task="ppt")
    direct = report.variants["direct"]
    aer = report.variants["aer"]
    assert report.overall.records == 3
    assert report.overall.successes == 2
    assert report.overall.success_rate == pytest.approx(2 / 3)
    assert direct.reported_total_tokens == 204
    assert direct.tokens_per_success == 137
    assert direct.model_calls_per_success == 8
    assert direct.tool_calls_per_success == 14
    assert direct.average_retries == 2
    assert direct.average_duration_ms == 750
    assert aer.tokens_per_success == 60
    assert direct.field_totals["cached_input_tokens"] == 120
    assert direct.as_dict()["measurement"]["cached_input_tokens_counted_separately"] is True

    comparison = profiles.compare("ppt")
    assert comparison.lowest_tokens_per_success_variant == "aer"
    assert comparison.differences_from_lowest["direct"]["absolute_more_than_lowest"] == 77
    assert comparison.as_dict()["provider_billed_tokens_known"] is False


def test_missing_provider_values_remain_explicitly_unknown(tmp_path: Path) -> None:
    profiles = ProfileStore(settings_for(tmp_path / "aer"))
    record = profiles.record(
        task="unknown-provider",
        variant="partial",
        success=True,
        input_tokens=100,
        output_tokens=20,
    )

    assert record.reported_total_tokens == 120
    assert set(record.missing_token_fields) == {
        "reasoning_tokens",
        "tool_schema_tokens",
        "tool_result_tokens",
    }
    report = profiles.report(task="unknown-provider")
    assert report.overall.tokens_per_success == 120
    assert report.overall.tokens_per_success_complete is False
    assert report.overall.missing_records["reasoning_tokens"] == 1
    assert profiles.compare("unknown-provider").lowest_tokens_per_success_variant is None


def test_export_has_stable_columns_and_atomic_symlink_policy(tmp_path: Path) -> None:
    profiles = ProfileStore(settings_for(tmp_path / "aer"))
    profiles.record(task="one", variant="a", success=True, input_tokens=1)
    profiles.record(task="two", variant="b", success=False, notes="kept exactly")
    output = tmp_path / "profile.csv"

    assert profiles.export(output) == 2
    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert tuple(rows[0]) == PROFILE_FIELDS
    assert {row["task"] for row in rows} == {"one", "two"}
    assert next(row for row in rows if row["task"] == "two")["notes"] == "kept exactly"

    symlink = tmp_path / "profile-link.csv"
    symlink.symlink_to(output)
    with pytest.raises(AerError) as rejected:
        profiles.export(symlink)
    assert rejected.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"task": "", "variant": "x", "success": True}, "INVALID_ARGUMENT"),
        (
            {"task": "x", "variant": "y", "success": True, "input_tokens": -1},
            "INVALID_ARGUMENT",
        ),
        (
            {"task": "x", "variant": "y", "success": True, "input_tokens": 1.5},
            "INVALID_ARGUMENT",
        ),
        (
            {
                "task": "x",
                "variant": "y",
                "success": True,
                "timestamp": "2026-08-31T00:00:00",
            },
            "INVALID_ARGUMENT",
        ),
    ],
)
def test_invalid_profile_values_are_rejected(
    tmp_path: Path,
    arguments: dict[str, object],
    code: str,
) -> None:
    profiles = ProfileStore(settings_for(tmp_path / "aer"))
    with pytest.raises(AerError) as error:
        profiles.record(**arguments)  # type: ignore[arg-type]
    assert error.value.code == code
