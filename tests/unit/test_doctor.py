from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import aer.doctor.engine as doctor_engine
from aer.config import Settings
from aer.doctor import DoctorCheck, DoctorResult, doctor_response, run_doctor
from aer.errors import AerError
from aer.store import ObjectStore


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


def _by_name(result: DoctorResult) -> dict[str, DoctorCheck]:
    return {check.name: check for check in result.checks}


def test_doctor_checks_core_services_and_cleans_store_probe(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "aer-home")
    result = run_doctor(settings)
    checks = _by_name(result)

    assert result.ok, [
        (check.name, check.message, check.details)
        for check in result.checks
        if check.required and not check.ok
    ]
    for name in ("python", "aer_home", "sqlite", "object_store", "recipes"):
        assert checks[name].required
        assert checks[name].ok, checks[name]
    assert checks["recipes"].details["count"] == 5
    assert checks["object_store"].details == {
        "existing_objects_verified": 0,
        "round_trip_bytes": 62,
        "cleanup": True,
    }
    assert checks["templates"].capabilities == ("presentation.build",)
    assert checks["templates"].details == {
        "available": ["business-clean"],
        "count": 1,
        "custom_templates_supported": False,
    }
    assert all("artifact.custom_template" not in check.capabilities for check in result.checks)

    store = ObjectStore(settings)
    assert store.list() == []
    assert list((settings.store_dir / "sha256").rglob("*[0-9a-f]")) == []
    assert list((settings.store_dir / ".locks").glob("*.lock")) == []
    assert list(settings.home.glob(".doctor-*")) == []


def test_doctor_detects_corrupt_existing_store_object(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "aer-home")
    store = ObjectStore(settings)
    record = store.put_bytes(b"original", filename="payload.bin")
    store.resolve_path(record.ref).write_bytes(b"tampered")

    result = run_doctor(settings)
    check = _by_name(result)["object_store"]

    assert result.ok is False
    assert check.ok is False
    assert "CORRUPT_FILE" in check.message
    assert "sha-256" in check.message.casefold()


def test_missing_optional_dependencies_do_not_fail_doctor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(doctor_engine.shutil, "which", lambda _name: None)
    monkeypatch.setattr(doctor_engine.importlib.util, "find_spec", lambda _name: None)

    result = run_doctor(_settings(tmp_path / "home"))
    optional = [check for check in result.checks if not check.required]
    dependency_checks = [
        check
        for check in optional
        if check.name.startswith("executable.") or check.name.startswith("python_library.")
    ]

    assert result.ok
    assert optional
    assert dependency_checks
    assert all(not check.ok for check in dependency_checks)
    assert all(check.status == "unavailable" for check in dependency_checks)
    assert _by_name(result)["executable.libreoffice"].capabilities == (
        "office.to_pdf",
        "artifact.validate.render",
    )
    assert "python_library.pyarrow" not in _by_name(result)
    assert all("data.parquet" not in check.capabilities for check in result.checks)


def test_doctor_response_is_compact_and_uses_common_protocol(tmp_path: Path) -> None:
    result = run_doctor(_settings(tmp_path / "home"))
    response = doctor_response(result)
    encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()

    assert response["ok"] is True
    assert response["operation"] == "doctor"
    assert len(encoded) < 16 * 1024


def test_old_python_is_a_required_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_engine.sys, "version_info", (3, 10, 14))
    check = doctor_engine._check_python()
    result = DoctorResult((check,))
    response = doctor_response(result)

    assert not result.ok
    assert check.required
    assert check.details == {"version": "3.10.14", "minimum": "3.11"}
    assert response["code"] == "VALIDATION_FAILED"
    assert response["failed_checks"] == ["python"]


def test_home_write_failure_is_reported_without_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_probe(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise PermissionError("denied")

    monkeypatch.setattr(doctor_engine.tempfile, "mkstemp", fail_probe)
    check = doctor_engine._check_home(_settings(tmp_path / "home"))

    assert not check.ok
    assert check.required
    assert check.status == "error"
    assert "denied" in check.message


def test_executable_check_records_path_version_and_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_engine.shutil, "which", lambda name: f"/tools/{name}")

    def completed(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["/tools/rg", "--version"], 0, "ripgrep 15.0.0\n")

    monkeypatch.setattr(doctor_engine.subprocess, "run", completed)
    check = doctor_engine._check_executable(
        "ripgrep", ("rg",), ("--version",), ("repository.inspect.search",)
    )

    assert check.ok
    assert check.details == {
        "path": "/tools/rg",
        "command": "rg",
        "version": "ripgrep 15.0.0",
    }
    assert check.capabilities == ("repository.inspect.search",)


def test_nonzero_executable_version_probe_is_optional_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_engine.shutil, "which", lambda _name: "/tools/pandoc")
    monkeypatch.setattr(
        doctor_engine.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["/tools/pandoc", "--version"], 2, "probe failed\n"
        ),
    )
    check = doctor_engine._check_executable(
        "pandoc", ("pandoc",), ("--version",), ("markup.convert",)
    )
    assert not check.ok
    assert not check.required
    assert check.details["version_output"] == "probe failed"


def test_invalid_builtin_recipe_is_a_core_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import aer.recipes

    class InvalidRecipeEngine:
        def __init__(self, _settings: Settings) -> None:
            pass

        def list(self) -> list[dict[str, object]]:
            return [
                {"name": name, "builtin": True} for name in sorted(doctor_engine._EXPECTED_RECIPES)
            ]

        def show(self, name: str) -> dict[str, object]:
            if name == "office-delivery":
                raise AerError("INVALID_SPEC", "broken recipe", "recipe.validate")
            return {"summary": {"step_count": 1, "input_count": 1}}

    monkeypatch.setattr(aer.recipes, "RecipeEngine", InvalidRecipeEngine)
    check = doctor_engine._check_recipes(_settings(tmp_path / "home"))

    assert not check.ok
    assert check.required
    invalid = check.details["invalid"]
    assert isinstance(invalid, list)
    assert invalid[0]["name"] == "office-delivery"


def test_store_probe_attempts_cleanup_after_verification_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    deleted: list[str] = []

    class Record:
        def __init__(self, ref: str, digest: str) -> None:
            self.ref = ref
            self.digest = digest

    class BrokenStore:
        def __init__(self, _settings: Settings) -> None:
            pass

        def stat(self, ref: str) -> Record:
            raise AerError("NOT_FOUND", "missing", "store.stat", ref)

        def list(self, *, limit: int, offset: int) -> list[Record]:
            del limit, offset
            return []

        def put_bytes(self, data: bytes, **_kwargs: object) -> Record:
            digest = doctor_engine.sha256_bytes(data)
            return Record(doctor_engine.format_ref(digest), digest)

        def get_bytes(self, _ref: str) -> bytes:
            return b"wrong"

        def verify(self, ref: str) -> Record:
            raise AssertionError(ref)

        def delete(self, ref: str, *, force: bool = False) -> bool:
            assert force
            deleted.append(ref)
            return True

    monkeypatch.setattr(doctor_engine, "ObjectStore", BrokenStore)
    check = doctor_engine._check_store(_settings(tmp_path / "home"))

    assert not check.ok
    assert check.required
    assert len(deleted) == 1
    assert check.details["cleanup"] is True
