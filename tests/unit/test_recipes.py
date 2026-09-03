from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
import yaml

from aer.config import Settings
from aer.errors import AerError
from aer.recipes import RecipeEngine


def _settings(home: Path) -> Settings:
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


def _recipe(path: Path, value: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def test_every_builtin_recipe_validates_and_dry_run_resolves_inputs(tmp_path: Path) -> None:
    engine = RecipeEngine(_settings(tmp_path / "home"))
    builtins = [item for item in engine.list() if item["builtin"]]
    assert {item["name"] for item in builtins} == {
        "data-extract",
        "office-delivery",
        "presentation-delivery",
        "project-package",
        "test-and-package",
    }
    for item in builtins:
        shown = engine.show(item["name"])
        assert shown["trusted"] is True
        assert shown["summary"]["step_count"] >= 1

    dry_run = engine.run(
        "office-delivery",
        variables={"spec": "/exact/spec.yaml", "output_dir": "/exact/output"},
        dry_run=True,
    )
    assert dry_run["dry_run"] is True
    assert dry_run["steps"][0]["with"]["spec"] == "/exact/spec.yaml"
    assert dry_run["steps"][0]["with"]["output"] == "/exact/output/report.docx"


def test_recipe_cycle_and_unknown_step_are_rejected(tmp_path: Path) -> None:
    engine = RecipeEngine(_settings(tmp_path / "home"))
    cycle = _recipe(
        tmp_path / "cycle.yaml",
        {
            "version": 1,
            "name": "cycle",
            "steps": [
                {
                    "id": "self",
                    "uses": "artifact.validate",
                    "with": {"target": "${{ steps.self.output }}"},
                }
            ],
        },
    )
    with pytest.raises(AerError) as cycle_error:
        engine.validate(cycle)
    assert cycle_error.value.code == "INVALID_SPEC"
    assert "cycle" in cycle_error.value.message.casefold()

    unknown = _recipe(
        tmp_path / "unknown.yaml",
        {
            "version": 1,
            "name": "unknown",
            "steps": [
                {
                    "id": "validate",
                    "uses": "artifact.validate",
                    "with": {"target": "${{ steps.missing.output }}"},
                }
            ],
        },
    )
    with pytest.raises(AerError, match="unknown step"):
        engine.validate(unknown)


def test_recipe_rejects_forward_references_and_missing_capability_arguments(
    tmp_path: Path,
) -> None:
    engine = RecipeEngine(_settings(tmp_path / "home"))
    forward = _recipe(
        tmp_path / "forward.yaml",
        {
            "version": 1,
            "name": "forward",
            "steps": [
                {
                    "id": "validate",
                    "uses": "artifact.validate",
                    "with": {"target": "${{ steps.build.output }}"},
                },
                {
                    "id": "build",
                    "uses": "document.build",
                    "with": {"spec": "document.yaml", "output": "document.docx"},
                },
            ],
        },
    )
    with pytest.raises(AerError, match="previously completed") as forward_error:
        engine.validate(forward)
    assert forward_error.value.details == {"forward": ["build"]}

    missing = _recipe(
        tmp_path / "missing-arguments.yaml",
        {
            "version": 1,
            "name": "missing-arguments",
            "steps": [{"id": "build", "uses": "document.build", "with": {"output": "x"}}],
        },
    )
    with pytest.raises(AerError, match="missing required") as missing_error:
        engine.validate(missing)
    assert missing_error.value.code == "INVALID_SPEC"
    assert missing_error.value.details == {
        "capability": "document.build",
        "missing": ["spec"],
    }

    typo = _recipe(
        tmp_path / "unknown-argument.yaml",
        {
            "version": 1,
            "name": "unknown-argument",
            "steps": [
                {
                    "id": "package",
                    "uses": "archive.create",
                    "with": {"source": "input", "output": "out.zip", "dryrun": True},
                }
            ],
        },
    )
    with pytest.raises(AerError, match="unknown capability") as typo_error:
        engine.validate(typo)
    assert typo_error.value.details["unknown"] == ["dryrun"]


def test_installed_recipe_filename_must_match_declared_name(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "home")
    recipe = _recipe(
        settings.recipes_dir / "filename.yaml",
        {
            "version": 1,
            "name": "different-name",
            "steps": [{"id": "store", "uses": "store.put", "with": {"source": "input"}}],
        },
    )
    engine = RecipeEngine(settings)

    assert all(item["name"] != "different-name" for item in engine.list())
    with pytest.raises(AerError, match="filename"):
        engine.show(recipe.stem)


def test_cyclic_yaml_recipe_is_rejected_as_invalid_spec(tmp_path: Path) -> None:
    recipe = tmp_path / "cycle-recipe.yaml"
    recipe.write_text(
        "version: 1\n"
        "name: cycle-recipe\n"
        "steps:\n"
        "  - id: validate\n"
        "    uses: artifact.validate\n"
        "    with: &args\n"
        "      target: example.zip\n"
        "      self: *args\n",
        encoding="utf-8",
    )

    with pytest.raises(AerError, match="Cyclic YAML aliases") as captured:
        RecipeEngine(_settings(tmp_path / "home")).validate(recipe)

    assert captured.value.code == "INVALID_SPEC"


def test_external_recipe_requires_explicit_trust_even_for_dry_run(tmp_path: Path) -> None:
    engine = RecipeEngine(_settings(tmp_path / "home"))
    external = _recipe(
        tmp_path / "external.yaml",
        {
            "version": 1,
            "name": "external",
            "inputs": {"source": {"type": "path"}},
            "steps": [
                {
                    "id": "store",
                    "uses": "store.put",
                    "with": {"source": "${{ inputs.source }}"},
                }
            ],
        },
    )
    with pytest.raises(AerError) as untrusted:
        engine.run(external, variables={"source": "/tmp/source"}, dry_run=True)
    assert untrusted.value.code == "UNTRUSTED_RECIPE"

    trusted = engine.run(
        external,
        variables={"source": "/tmp/source"},
        dry_run=True,
        trust=True,
    )
    assert trusted["inputs"] == {"source": "/tmp/source"}


def test_external_raw_command_requires_both_explicit_permissions(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "home")
    external = _recipe(
        tmp_path / "external-command.yaml",
        {
            "version": 1,
            "name": "external-command",
            "steps": [
                {
                    "id": "command",
                    "uses": "command.run",
                    "with": {"argv": [sys.executable, "-c", "print('trusted')"]},
                }
            ],
        },
    )

    result = RecipeEngine(settings).run(
        external,
        variables={},
        trust=True,
        allow_raw_command=True,
    )

    assert result["success"] is True
    assert result["steps"][0]["result"]["summary"] == "trusted"


def test_raw_command_recipes_are_never_served_from_cache(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "home")
    counter = tmp_path / "count.txt"
    recipe = _recipe(
        settings.recipes_dir / "uncached-command.yaml",
        {
            "version": 1,
            "name": "uncached-command",
            "steps": [
                {
                    "id": "command",
                    "uses": "command.run",
                    "with": {
                        "argv": [
                            sys.executable,
                            "-c",
                            "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_text((p.read_text() if p.exists() else '')+'x')",
                            str(counter),
                        ]
                    },
                }
            ],
        },
    )
    engine = RecipeEngine(settings)

    first = engine.run(recipe.stem, variables={}, allow_raw_command=True)
    second = engine.run(recipe.stem, variables={}, allow_raw_command=True)

    assert first["cache_hit"] is False
    assert second["cache_hit"] is False
    assert counter.read_text() == "xx"


def test_raw_command_is_denied_by_default_and_shell_metacharacters_are_literal(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "home")
    marker = tmp_path / "injected"
    recipe = _recipe(
        settings.recipes_dir / "raw.yaml",
        {
            "version": 1,
            "name": "raw",
            "inputs": {"argument": {"type": "string"}},
            "steps": [
                {
                    "id": "command",
                    "uses": "command.run",
                    "with": {
                        "argv": [
                            sys.executable,
                            "-c",
                            "import sys; print(sys.argv[1])",
                            "${{ inputs.argument }}",
                        ]
                    },
                }
            ],
        },
    )
    engine = RecipeEngine(settings)
    hostile = f"; touch {marker}"
    with pytest.raises(AerError) as denied:
        engine.run(recipe.stem, variables={"argument": hostile})
    assert denied.value.code == "UNTRUSTED_RECIPE"

    result = engine.run(
        recipe.stem,
        variables={"argument": hostile},
        allow_raw_command=True,
    )
    assert result["success"] is True
    command = result["steps"][0]["result"]
    assert command["summary"] == hostile
    assert not marker.exists()


def test_recipe_command_timeout_is_enforced(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "home")
    _recipe(
        settings.recipes_dir / "slow.yaml",
        {
            "version": 1,
            "name": "slow",
            "steps": [
                {
                    "id": "slow-command",
                    "uses": "command.run",
                    "with": {"argv": [sys.executable, "-c", "import time; time.sleep(30)"]},
                }
            ],
        },
    )
    started = time.monotonic()
    with pytest.raises(AerError) as timeout:
        RecipeEngine(settings).run(
            "slow",
            variables={},
            allow_raw_command=True,
            timeout=0.15,
        )
    assert time.monotonic() - started < 3
    assert timeout.value.code == "COMMAND_TIMEOUT"
    assert timeout.value.details["timed_out"] is True
    assert timeout.value.details["failed_step"] == "slow-command"
    assert timeout.value.details["recipe_log_ref"].startswith("aer://sha256/")
    assert timeout.value.raw_ref is not None


def test_recipe_overall_timeout_interrupts_non_command_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path / "home")
    _recipe(
        settings.recipes_dir / "slow-local.yaml",
        {
            "version": 1,
            "name": "slow-local",
            "steps": [
                {
                    "id": "slow-store",
                    "uses": "store.put",
                    "with": {"source": str(tmp_path / "unused")},
                }
            ],
        },
    )
    engine = RecipeEngine(settings)

    def slow_dispatch(*_args, **_kwargs):
        time.sleep(0.2)
        return {"output": str(tmp_path / "late")}

    monkeypatch.setattr(engine, "_dispatch", slow_dispatch)
    started = time.monotonic()

    with pytest.raises(AerError) as timeout:
        engine.run("slow-local", variables={}, timeout=0.05)

    assert time.monotonic() - started < 1
    assert timeout.value.code == "COMMAND_TIMEOUT"
    assert timeout.value.details["failed_step"] == "slow-store"


def test_recipe_cache_uses_input_content_and_existing_step_output(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "home")
    source = tmp_path / "source.csv"
    source.write_text("id,value\n1,10\n2,20\n", encoding="utf-8")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _recipe(
        settings.recipes_dir / "cached-query.yaml",
        {
            "version": 1,
            "name": "cached-query",
            "inputs": {
                "source": {"type": "path"},
                "output_dir": {"type": "path"},
            },
            "steps": [
                {
                    "id": "query",
                    "uses": "data.query",
                    "with": {
                        "source": "${{ inputs.source }}",
                        "where": ["value >= 10"],
                        "output": "${{ inputs.output_dir }}/result.csv",
                    },
                }
            ],
        },
    )
    variables = {"source": str(source), "output_dir": str(output_dir)}
    engine = RecipeEngine(settings)
    first = engine.run("cached-query", variables=variables)
    second = engine.run("cached-query", variables=variables)
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True

    output_file = output_dir / "result.csv"
    output_file.write_text("tampered", encoding="utf-8")
    repaired = engine.run("cached-query", variables=variables)
    assert repaired["cache_hit"] is False
    assert "tampered" not in output_file.read_text(encoding="utf-8")

    source.write_text("id,value\n1,99\n", encoding="utf-8")
    invalidated = engine.run("cached-query", variables=variables)
    assert invalidated["cache_hit"] is False


def test_recipe_with_literal_content_path_bypasses_cache(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "home")
    source = tmp_path / "literal.csv"
    source.write_text("id,value\n1,10\n", encoding="utf-8")
    output = tmp_path / "literal-output.csv"
    _recipe(
        settings.recipes_dir / "literal-query.yaml",
        {
            "version": 1,
            "name": "literal-query",
            "steps": [
                {
                    "id": "query",
                    "uses": "data.query",
                    "with": {"source": str(source), "output": str(output)},
                }
            ],
        },
    )
    engine = RecipeEngine(settings)

    first = engine.run("literal-query", variables={})
    source.write_text("id,value\n1,99\n", encoding="utf-8")
    second = engine.run("literal-query", variables={})

    assert first["cache_hit"] is False
    assert second["cache_hit"] is False
    assert "99" in output.read_text(encoding="utf-8")


def test_recipe_cache_invalidates_when_source_directory_content_changes(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "home")
    source = tmp_path / "project"
    source.mkdir()
    (source / "payload.txt").write_text("first", encoding="utf-8")
    output = tmp_path / "project.zip"
    engine = RecipeEngine(settings)
    variables = {"source": str(source), "output": str(output)}

    first = engine.run("project-package", variables=variables)
    second = engine.run("project-package", variables=variables)
    (source / "payload.txt").write_text("second", encoding="utf-8")
    output.unlink()
    third = engine.run("project-package", variables=variables)

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert third["cache_hit"] is False


def test_recipe_typed_inputs_reject_invalid_values(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "home")
    _recipe(
        settings.recipes_dir / "typed.yaml",
        {
            "version": 1,
            "name": "typed",
            "inputs": {
                "attempts": {"type": "integer"},
                "strict": {"type": "boolean"},
            },
            "steps": [
                {
                    "id": "store",
                    "uses": "store.put",
                    "with": {"source": "unused"},
                }
            ],
        },
    )
    with pytest.raises(AerError) as invalid:
        RecipeEngine(settings).run(
            "typed",
            variables={"attempts": "not-an-int", "strict": "perhaps"},
            dry_run=True,
        )
    assert invalid.value.code == "INVALID_ARGUMENT"
