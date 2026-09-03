"""Compact diagnostics for AER core services and optional capabilities."""

from __future__ import annotations

import importlib.util
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from importlib import metadata, resources
from pathlib import Path
from typing import Any

from aer.config import Settings
from aer.errors import AerError
from aer.hashing import sha256_bytes
from aer.protocol import success
from aer.store import ObjectStore, format_ref

_MINIMUM_PYTHON = (3, 11)
_MAX_DETAIL_LENGTH = 300
_EXPECTED_RECIPES = {
    "data-extract",
    "office-delivery",
    "presentation-delivery",
    "project-package",
    "test-and-package",
}

_EXECUTABLES: tuple[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "libreoffice",
        ("libreoffice", "soffice"),
        ("--version",),
        ("office.to_pdf", "artifact.validate.render"),
    ),
    ("pandoc", ("pandoc",), ("--version",), ("markup.convert",)),
    (
        "pdftoppm",
        ("pdftoppm",),
        ("-v",),
        ("pdf.rasterize", "artifact.validate.render"),
    ),
    ("git", ("git",), ("--version",), ("repository.inspect.git",)),
    ("ripgrep", ("rg",), ("--version",), ("repository.inspect.search",)),
)

_PYTHON_DEPENDENCIES: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("python-pptx", "pptx", ("python-pptx",), ("presentation",)),
    ("python-docx", "docx", ("python-docx",), ("document",)),
    ("openpyxl", "openpyxl", ("openpyxl",), ("workbook", "data.xlsx")),
    ("pypdf", "pypdf", ("pypdf",), ("pdf",)),
    ("Pillow", "PIL", ("Pillow",), ("image",)),
    ("matplotlib", "matplotlib", ("matplotlib",), ("chart",)),
)


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One independently actionable diagnostic result."""

    name: str
    ok: bool
    required: bool
    status: str
    message: str
    capabilities: tuple[str, ...] = ()
    details: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["capabilities"] = list(self.capabilities)
        return payload


@dataclass(frozen=True, slots=True)
class DoctorResult:
    """Complete doctor result with optional failures separated from core health."""

    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks if check.required)

    def to_dict(self) -> dict[str, object]:
        required = [check for check in self.checks if check.required]
        optional = [check for check in self.checks if not check.required]
        return {
            "ok": self.ok,
            "summary": {
                "core_passed": sum(check.ok for check in required),
                "core_failed": sum(not check.ok for check in required),
                "optional_available": sum(check.ok for check in optional),
                "optional_unavailable": sum(not check.ok for check in optional),
            },
            "checks": [check.to_dict() for check in self.checks],
        }


def _compact(value: object) -> str:
    text = " ".join(str(value).split()) or type(value).__name__
    if len(text) <= _MAX_DETAIL_LENGTH:
        return text
    return text[: _MAX_DETAIL_LENGTH - 3] + "..."


def _failed(
    name: str,
    message: str,
    *,
    required: bool,
    capabilities: Sequence[str] = (),
    details: Mapping[str, object] | None = None,
) -> DoctorCheck:
    return DoctorCheck(
        name=name,
        ok=False,
        required=required,
        status="error" if required else "unavailable",
        message=_compact(message),
        capabilities=tuple(capabilities),
        details=dict(details or {}),
    )


def _check_python() -> DoctorCheck:
    version = tuple(sys.version_info[:3])
    ok = version >= _MINIMUM_PYTHON
    return DoctorCheck(
        name="python",
        ok=ok,
        required=True,
        status="ok" if ok else "error",
        message=(
            "Supported Python runtime."
            if ok
            else f"Python {_MINIMUM_PYTHON[0]}.{_MINIMUM_PYTHON[1]} or newer is required."
        ),
        details={"version": ".".join(map(str, version)), "minimum": "3.11"},
    )


def _check_home(settings: Settings) -> DoctorCheck:
    descriptor: int | None = None
    probe_path: Path | None = None
    cleanup_error: OSError | None = None
    try:
        settings.ensure()
        descriptor, probe_name = tempfile.mkstemp(prefix=".doctor-", dir=settings.home)
        probe_path = Path(probe_name)
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, b"aer-doctor-write-probe")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
    except OSError as exc:
        return _failed(
            "aer_home",
            f"AER_HOME is not writable: {exc}",
            required=True,
            details={"path": str(settings.home)},
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_error = exc
    if cleanup_error is not None:
        return _failed(
            "aer_home",
            f"AER_HOME probe could not be removed: {cleanup_error}",
            required=True,
            details={"path": str(settings.home)},
        )
    return DoctorCheck(
        name="aer_home",
        ok=True,
        required=True,
        status="ok",
        message="AER_HOME is writable.",
        details={"path": str(settings.home.resolve())},
    )


def _check_sqlite(settings: Settings) -> DoctorCheck:
    try:
        with settings.connect() as connection:
            version_row = connection.execute("SELECT sqlite_version()").fetchone()
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            connection.execute("CREATE TEMP TABLE aer_doctor_probe (value INTEGER NOT NULL)")
            connection.execute("INSERT INTO aer_doctor_probe VALUES (1)")
            probe_row = connection.execute("SELECT value FROM aer_doctor_probe").fetchone()
        version = str(version_row[0]) if version_row is not None else sqlite3.sqlite_version
        integrity = str(quick_check[0]) if quick_check is not None else "unknown"
        if integrity.casefold() != "ok" or probe_row is None or probe_row[0] != 1:
            return _failed(
                "sqlite",
                "SQLite integrity or read/write probe failed.",
                required=True,
                details={"version": version, "integrity": integrity},
            )
        return DoctorCheck(
            name="sqlite",
            ok=True,
            required=True,
            status="ok",
            message="SQLite is available and the AER database passed quick_check.",
            details={"version": version, "integrity": integrity},
        )
    except (OSError, sqlite3.Error) as exc:
        return _failed(
            "sqlite",
            f"SQLite probe failed: {exc}",
            required=True,
            details={"database": str(settings.database)},
        )


def _remove_store_probe_scaffolding(settings: Settings, digest: str) -> None:
    """Remove only empty directories and the lock belonging to this random probe."""

    root = settings.store_dir.resolve()
    lock_path = root / ".locks" / f"{digest}.lock"
    lock_path.unlink(missing_ok=True)
    leaf = root / "sha256" / digest[:2] / digest[2:4]
    for directory in (leaf, leaf.parent):
        try:
            directory.rmdir()
        except FileNotFoundError:
            continue
        except OSError:
            # It contains an unrelated object and therefore must remain.
            break


def _check_store(settings: Settings) -> DoctorCheck:
    store: ObjectStore | None = None
    ref: str | None = None
    digest: str | None = None
    put_attempted = False
    inserted = False
    error: BaseException | None = None
    cleanup_error: BaseException | None = None
    existing_verified = 0
    try:
        store = ObjectStore(settings)
        offset = 0
        while True:
            records = store.list(limit=1000, offset=offset)
            if not records:
                break
            for existing in records:
                store.verify(existing.ref)
                existing_verified += 1
            offset += len(records)
        for _ in range(3):
            payload = b"aer-doctor-v1\0" + secrets.token_bytes(48)
            digest = sha256_bytes(payload)
            ref = format_ref(digest)
            try:
                store.stat(ref)
            except AerError as exc:
                if exc.code == "NOT_FOUND":
                    break
                raise
        else:  # pragma: no cover - a cryptographic collision is not realistically reachable
            raise AerError(
                "CONFLICT",
                "Could not allocate a unique doctor probe object.",
                operation="doctor",
            )
        put_attempted = True
        record = store.put_bytes(
            payload,
            filename="doctor-probe.bin",
            mime_type="application/octet-stream",
            source={"operation": "doctor", "temporary": True},
        )
        inserted = True
        if record.ref != ref or store.get_bytes(record.ref) != payload:
            raise AerError(
                "CORRUPT_FILE",
                "Object-store round-trip returned different content.",
                operation="doctor",
                target=record.ref,
            )
        verified = store.verify(record.ref)
        if verified.digest != digest:
            raise AerError(
                "CORRUPT_FILE",
                "Object-store verification returned a different digest.",
                operation="doctor",
                target=record.ref,
            )
    except BaseException as exc:  # converted into a compact doctor check below
        error = exc
    finally:
        if store is not None and ref is not None and put_attempted:
            try:
                deleted = store.delete(ref, force=True)
                if inserted and not deleted:
                    raise RuntimeError("temporary object was not deleted")
            except BaseException as exc:  # cleanup failure is a core health failure
                cleanup_error = exc
        if digest is not None:
            try:
                _remove_store_probe_scaffolding(settings, digest)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
    if error is not None or cleanup_error is not None:
        reason = error or cleanup_error
        return _failed(
            "object_store",
            f"Object-store round-trip failed: {_compact(reason)}",
            required=True,
            details={"cleanup": cleanup_error is None},
        )
    return DoctorCheck(
        name="object_store",
        ok=True,
        required=True,
        status="ok",
        message="Object store passed existing-object verification and round-trip probes.",
        details={
            "existing_objects_verified": existing_verified,
            "round_trip_bytes": len(payload),
            "cleanup": True,
        },
    )


def _check_templates() -> DoctorCheck:
    try:
        directory = resources.files("aer").joinpath("resources/templates")
        names = sorted(
            item.name.removesuffix(".json")
            for item in directory.iterdir()
            if item.is_file() and item.name == "business-clean.json"
        )
    except (FileNotFoundError, ModuleNotFoundError, NotADirectoryError) as exc:
        return _failed(
            "templates",
            f"No packaged built-in theme metadata was found: {exc}",
            required=False,
            details={"available": [], "custom_templates_supported": False},
        )
    if not names:
        return DoctorCheck(
            name="templates",
            ok=False,
            required=False,
            status="unavailable",
            message="Packaged business-clean theme metadata is unavailable.",
            details={"available": [], "custom_templates_supported": False},
        )
    return DoctorCheck(
        name="templates",
        ok=True,
        required=False,
        status="ok",
        message="Packaged built-in theme metadata is available.",
        capabilities=("presentation.build",),
        details={
            "available": names,
            "count": len(names),
            "custom_templates_supported": False,
        },
    )


def _check_recipes(settings: Settings) -> DoctorCheck:
    try:
        from aer.recipes import RecipeEngine

        engine = RecipeEngine(settings)
        records = engine.list()
        names = {str(record["name"]) for record in records if record.get("builtin") is True}
        missing = sorted(_EXPECTED_RECIPES - names)
        invalid: list[dict[str, str]] = []
        summaries: list[dict[str, object]] = []
        for name in sorted(names):
            try:
                shown = engine.show(name)
                summary = shown["summary"]
                summaries.append(
                    {
                        "name": name,
                        "steps": summary["step_count"],
                        "inputs": summary["input_count"],
                    }
                )
            except (AerError, OSError, KeyError, TypeError) as exc:
                invalid.append({"name": name, "error": _compact(exc)})
        if missing or invalid:
            return _failed(
                "recipes",
                "One or more built-in recipes are missing or invalid.",
                required=True,
                capabilities=("recipe.run",),
                details={"missing": missing, "invalid": invalid, "valid": summaries},
            )
        return DoctorCheck(
            name="recipes",
            ok=True,
            required=True,
            status="ok",
            message="Every built-in recipe is valid.",
            capabilities=("recipe.run",),
            details={"count": len(summaries), "recipes": summaries},
        )
    except (AerError, OSError, ModuleNotFoundError) as exc:
        return _failed(
            "recipes",
            f"Built-in recipes could not be checked: {exc}",
            required=True,
            capabilities=("recipe.run",),
        )


def _first_line(value: str) -> str | None:
    for line in value.splitlines():
        if line.strip():
            return _compact(line)
    return None


def _check_executable(
    name: str,
    candidates: Sequence[str],
    version_arguments: Sequence[str],
    capabilities: Sequence[str],
) -> DoctorCheck:
    executable: str | None = None
    selected: str | None = None
    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable is not None:
            selected = candidate
            break
    if executable is None:
        return _failed(
            f"executable.{name}",
            f"Optional executable is not installed: {name}",
            required=False,
            capabilities=capabilities,
            details={"candidates": list(candidates)},
        )
    try:
        completed = subprocess.run(
            [executable, *version_arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=5,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _failed(
            f"executable.{name}",
            f"Executable version probe failed: {exc}",
            required=False,
            capabilities=capabilities,
            details={"path": executable},
        )
    version = _first_line(completed.stdout)
    if completed.returncode != 0:
        return _failed(
            f"executable.{name}",
            f"Executable version probe exited with {completed.returncode}.",
            required=False,
            capabilities=capabilities,
            details={"path": executable, "version_output": version},
        )
    return DoctorCheck(
        name=f"executable.{name}",
        ok=True,
        required=False,
        status="ok",
        message=f"Optional executable is available: {name}",
        capabilities=tuple(capabilities),
        details={"path": executable, "command": selected, "version": version},
    )


def _distribution_version(distributions: Sequence[str]) -> str | None:
    for distribution in distributions:
        try:
            return metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
    return None


def _check_python_dependency(
    name: str,
    module: str,
    distributions: Sequence[str],
    capabilities: Sequence[str],
) -> DoctorCheck:
    try:
        available = importlib.util.find_spec(module) is not None
    except (ImportError, AttributeError, ValueError):
        available = False
    version = _distribution_version(distributions) if available else None
    if not available:
        return _failed(
            f"python_library.{name}",
            f"Optional Python library is not installed: {name}",
            required=False,
            capabilities=capabilities,
        )
    return DoctorCheck(
        name=f"python_library.{name}",
        ok=True,
        required=False,
        status="ok",
        message=f"Optional Python library is available: {name}",
        capabilities=tuple(capabilities),
        details={"module": module, "version": version},
    )


def run_doctor(settings: Settings | None = None) -> DoctorResult:
    """Run all diagnostics without failing overall for optional dependencies."""

    configured = settings or Settings.load()
    checks: list[DoctorCheck] = [
        _check_python(),
        _check_home(configured),
        _check_sqlite(configured),
        _check_store(configured),
        _check_templates(),
        _check_recipes(configured),
    ]
    checks.extend(
        _check_executable(name, candidates, arguments, capabilities)
        for name, candidates, arguments, capabilities in _EXECUTABLES
    )
    checks.extend(
        _check_python_dependency(name, module, distributions, capabilities)
        for name, module, distributions, capabilities in _PYTHON_DEPENDENCIES
    )
    return DoctorResult(tuple(checks))


def doctor_response(result: DoctorResult) -> dict[str, Any]:
    """Convert diagnostics into the shared response protocol."""

    details = result.to_dict()
    if result.ok:
        return success("doctor", details)
    failed_names = [check.name for check in result.checks if check.required and not check.ok]
    return {
        "ok": False,
        "operation": "doctor",
        "code": "VALIDATION_FAILED",
        "message": "One or more required AER health checks failed.",
        "target": None,
        "details": details,
        "suggested_action": "Resolve the failed required checks and run aer doctor again.",
        "raw_ref": None,
        "failed_checks": failed_names,
    }
