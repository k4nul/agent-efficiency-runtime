from __future__ import annotations

import math
from pathlib import Path

import pytest

from aer.benchmark import (
    SCENARIOS,
    TOKEN_ESTIMATION_METHOD,
    BenchmarkEngine,
    estimate_tokens,
)
from aer.config import Settings
from aer.errors import AerError


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


@pytest.mark.parametrize("byte_count", [0, 1, 4, 5, 101, 10_003])
def test_token_estimate_is_derived_from_bytes(byte_count: int) -> None:
    assert estimate_tokens(byte_count) == math.ceil(byte_count / 4)


def test_all_six_benchmarks_execute_measure_and_persist(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "aer")
    engine = BenchmarkEngine(settings)
    run = engine.run()

    assert tuple(result.scenario for result in run.scenarios) == SCENARIOS
    assert run.success is True
    assert run.duration_ms > 0
    for result in run.scenarios:
        assert result.success is True, result.error
        assert result.direct is not None
        assert result.aer is not None
        for measurement in (result.direct, result.aer):
            assert measurement.input_bytes > 0
            assert measurement.output_bytes > 0
            assert measurement.context_bytes > 0
            assert measurement.estimated_tokens == math.ceil(measurement.context_bytes / 4)
            assert measurement.estimation_method == TOKEN_ESTIMATION_METHOD
            assert measurement.not_provider_billed_tokens is True
            assert measurement.wall_time_ms >= 0
            assert measurement.retries == 0
            assert measurement.valid is True
            assert len(measurement.sha256) == 64
        assert result.context_bytes_saved == result.direct.context_bytes - result.aer.context_bytes
        assert (
            result.estimated_tokens_saved
            == result.direct.estimated_tokens - result.aer.estimated_tokens
        )

    by_name = {result.scenario: result for result in run.scenarios}
    log_result = by_name["log-compaction"]
    assert log_result.direct is not None and log_result.aer is not None
    assert log_result.direct.details["lines"] == 5_001
    assert log_result.aer.output_bytes < 16 * 1024
    assert isinstance(log_result.aer.details["raw_ref"], str)

    data_result = by_name["data-query"]
    assert data_result.aer is not None
    assert data_result.aer.details["source_rows"] == 10_000
    assert data_result.aer.details["matched_rows"] == 5_000
    assert data_result.aer.details["preview_rows"] <= 20

    json_result = by_name["json-patch"]
    assert json_result.aer is not None
    assert json_result.aer.details["preserved_equal"] is True

    ppt_result = by_name["presentation-patch"]
    assert ppt_result.aer is not None
    assert ppt_result.aer.details["unrelated_text_preserved"] is True

    recipe_result = by_name["recipe-package"]
    assert recipe_result.aer is not None
    assert recipe_result.aer.details["byte_identical_to_direct"] is True

    persisted = BenchmarkEngine(settings).report()
    assert len(persisted) == 1
    assert persisted[0].run_id == run.run_id
    assert persisted[0].as_dict() == run.as_dict()


def test_single_scenario_and_invalid_name(tmp_path: Path) -> None:
    engine = BenchmarkEngine(settings_for(tmp_path / "aer"))
    run = engine.run(scenario="data-query")
    assert len(run.scenarios) == 1
    assert run.scenarios[0].scenario == "data-query"
    assert run.success is True

    with pytest.raises(AerError) as invalid:
        engine.run(scenario="invented-result")
    assert invalid.value.code == "INVALID_ARGUMENT"
    assert invalid.value.details["available"] == list(SCENARIOS)


def test_report_rejects_unbounded_limit(tmp_path: Path) -> None:
    engine = BenchmarkEngine(settings_for(tmp_path / "aer"))
    with pytest.raises(AerError) as invalid:
        engine.report(limit=0)
    assert invalid.value.code == "INVALID_ARGUMENT"
