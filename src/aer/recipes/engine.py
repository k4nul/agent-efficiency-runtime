from __future__ import annotations

import json
import platform
import re
import signal
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any, cast

from aer.archive import create_archive, verify_archive
from aer.artifacts import build_artifact
from aer.cache import ContentHashCache
from aer.config import Settings
from aer.conversion import convert_file
from aer.data import query_data
from aer.errors import AerError
from aer.hashing import normalized_hash, sha256_directory, sha256_file
from aer.image import crop_image, fit_image, resize_image
from aer.limits import MAX_SPEC_FILE_BYTES
from aer.patch import apply_patch
from aer.pdf import extract_pdf, merge_pdfs, split_pdf
from aer.runner import run_command
from aer.store import ObjectStore
from aer.validation import validate_file
from aer.yaml_safety import load_yaml_safely

EXPRESSION = re.compile(r"\$\{\{\s*(inputs|steps)\.([A-Za-z0-9_-]+)(?:\.([A-Za-z0-9_-]+))?\s*\}\}")
ALLOWED_CAPABILITIES = {
    "presentation.build",
    "document.build",
    "workbook.build",
    "chart.build",
    "artifact.build",
    "artifact.patch",
    "artifact.validate",
    "archive.create",
    "archive.verify",
    "data.query",
    "artifact.convert",
    "image.resize",
    "image.crop",
    "image.fit",
    "pdf.merge",
    "pdf.extract",
    "pdf.split",
    "store.put",
    "command.run",
}

REQUIRED_CAPABILITY_ARGUMENTS: dict[str, frozenset[str]] = {
    "presentation.build": frozenset({"spec", "output"}),
    "document.build": frozenset({"spec", "output"}),
    "workbook.build": frozenset({"spec", "output"}),
    "chart.build": frozenset({"spec", "output"}),
    "artifact.build": frozenset({"spec", "output"}),
    "artifact.patch": frozenset({"target", "spec"}),
    "artifact.validate": frozenset({"target"}),
    "archive.create": frozenset({"source", "output"}),
    "archive.verify": frozenset({"target"}),
    "data.query": frozenset({"source"}),
    "artifact.convert": frozenset({"source", "output"}),
    "image.resize": frozenset({"source", "output"}),
    "image.crop": frozenset({"source", "output", "x", "y", "width", "height"}),
    "image.fit": frozenset({"source", "output", "ratio"}),
    "pdf.merge": frozenset({"inputs", "output"}),
    "pdf.extract": frozenset({"source", "output", "pages"}),
    "pdf.split": frozenset({"source", "output_dir"}),
    "store.put": frozenset({"source"}),
    "command.run": frozenset({"argv"}),
}

ALLOWED_CAPABILITY_ARGUMENTS: dict[str, frozenset[str]] = {
    **{
        name: frozenset({"spec", "output", "dry_run"})
        for name in {
            "presentation.build",
            "document.build",
            "workbook.build",
            "chart.build",
            "artifact.build",
        }
    },
    "artifact.patch": frozenset(
        {"target", "spec", "dry_run", "backup", "expected_sha256", "validate"}
    ),
    "artifact.validate": frozenset({"target", "strict", "render"}),
    "archive.create": frozenset({"source", "output", "exclude", "dry_run"}),
    "archive.verify": frozenset({"target"}),
    "data.query": frozenset(
        {
            "source",
            "sheet",
            "where",
            "select",
            "rename",
            "sort",
            "descending",
            "limit",
            "offset",
            "unique",
            "group_by",
            "aggregates",
            "duplicate_columns",
            "output",
        }
    ),
    "artifact.convert": frozenset({"source", "output"}),
    "image.resize": frozenset(
        {"source", "output", "width", "height", "strip_metadata", "overwrite"}
    ),
    "image.crop": frozenset(
        {"source", "output", "x", "y", "width", "height", "strip_metadata", "overwrite"}
    ),
    "image.fit": frozenset({"source", "output", "ratio", "mode", "width", "overwrite"}),
    "pdf.merge": frozenset({"inputs", "output"}),
    "pdf.extract": frozenset({"source", "output", "pages"}),
    "pdf.split": frozenset({"source", "output_dir"}),
    "store.put": frozenset({"source", "pin"}),
    "command.run": frozenset({"argv", "cwd", "timeout"}),
}

CONTENT_CAPABILITY_ARGUMENTS: dict[str, frozenset[str]] = {
    **{
        name: frozenset({"spec"})
        for name in {
            "presentation.build",
            "document.build",
            "workbook.build",
            "chart.build",
            "artifact.build",
        }
    },
    "artifact.patch": frozenset({"target", "spec"}),
    "artifact.validate": frozenset({"target"}),
    "archive.create": frozenset({"source"}),
    "archive.verify": frozenset({"target"}),
    "data.query": frozenset({"source"}),
    "artifact.convert": frozenset({"source"}),
    "image.resize": frozenset({"source"}),
    "image.crop": frozenset({"source"}),
    "image.fit": frozenset({"source"}),
    "pdf.merge": frozenset({"inputs"}),
    "pdf.extract": frozenset({"source"}),
    "pdf.split": frozenset({"source"}),
    "store.put": frozenset({"source"}),
}


def _read_yaml(text: str, target: str) -> dict[str, Any]:
    value = load_yaml_safely(text, operation="recipe.validate", target=target)
    if not isinstance(value, dict):
        raise AerError("INVALID_SPEC", "Recipe root must be an object.", "recipe.validate", target)
    return value


def _read_recipe_path(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AerError("NOT_FOUND", "Recipe file does not exist.", "recipe", str(path)) from exc
    if size > MAX_SPEC_FILE_BYTES:
        raise AerError(
            "LIMIT_EXCEEDED",
            "Recipe exceeds the size limit.",
            "recipe.validate",
            str(path),
            {"bytes": size, "limit": MAX_SPEC_FILE_BYTES},
        )
    try:
        return _read_yaml(path.read_text(encoding="utf-8"), str(path))
    except (OSError, UnicodeError) as exc:
        raise AerError(
            "INVALID_SPEC", "Recipe must be readable UTF-8.", "recipe.validate", str(path)
        ) from exc


def _validate_installed_name(recipe: dict[str, Any], path: Path) -> None:
    if recipe.get("name") != path.stem:
        raise AerError(
            "INVALID_SPEC",
            "Installed recipe filename must match its declared name.",
            "recipe.validate",
            f"{path}#/name",
            {"expected": path.stem, "actual": recipe.get("name")},
        )


def _expression_matches(value: Any) -> list[tuple[str, str, str | None]]:
    if isinstance(value, str):
        return [
            (match.group(1), match.group(2), match.group(3)) for match in EXPRESSION.finditer(value)
        ]
    if isinstance(value, list):
        return [match for item in value for match in _expression_matches(item)]
    if isinstance(value, dict):
        return [match for item in value.values() for match in _expression_matches(item)]
    return []


def _references(value: Any) -> set[str]:
    return {name for namespace, name, _field in _expression_matches(value) if namespace == "steps"}


def _is_exact_expression(value: Any) -> bool:
    if isinstance(value, str):
        return EXPRESSION.fullmatch(value) is not None
    if isinstance(value, list):
        return bool(value) and all(_is_exact_expression(item) for item in value)
    return False


def _recipe_cacheable(recipe: dict[str, Any]) -> bool:
    for step in recipe.get("steps", []):
        capability = str(step.get("uses", ""))
        if capability == "command.run":
            return False
        arguments = step.get("with", {})
        if not isinstance(arguments, dict):
            return False
        for key in CONTENT_CAPABILITY_ARGUMENTS.get(capability, frozenset()):
            if key in arguments and not _is_exact_expression(arguments[key]):
                return False
    return True


def _validate_step_arguments(
    capability: str,
    arguments: object,
    *,
    declared_inputs: set[str],
    target: str,
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AerError(
            "INVALID_SPEC",
            "Recipe step 'with' must be an object.",
            "recipe.validate",
            target,
        )
    missing = REQUIRED_CAPABILITY_ARGUMENTS[capability] - arguments.keys()
    if missing:
        raise AerError(
            "INVALID_SPEC",
            "Recipe step is missing required capability arguments.",
            "recipe.validate",
            target,
            {"capability": capability, "missing": sorted(missing)},
        )
    unknown = arguments.keys() - ALLOWED_CAPABILITY_ARGUMENTS[capability]
    if unknown:
        raise AerError(
            "INVALID_SPEC",
            "Recipe step contains unknown capability arguments.",
            "recipe.validate",
            target,
            {"capability": capability, "unknown": sorted(unknown)},
        )
    for namespace, name, field in _expression_matches(arguments):
        if namespace == "inputs" and (field is not None or name not in declared_inputs):
            raise AerError(
                "INVALID_SPEC",
                "Recipe input expression is invalid.",
                "recipe.validate",
                target,
                {"expression": f"inputs.{name}" if field is None else f"inputs.{name}.{field}"},
            )
    if capability == "image.resize" and not ({"width", "height"} & arguments.keys()):
        raise AerError(
            "INVALID_SPEC",
            "image.resize requires width or height.",
            "recipe.validate",
            target,
        )
    if capability == "command.run":
        argv = arguments["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(item, str) for item in argv)
        ):
            raise AerError(
                "INVALID_SPEC",
                "command.run argv must be a non-empty string array.",
                "recipe.validate",
                target,
            )
    if capability == "pdf.merge":
        sources = arguments["inputs"]
        if not isinstance(sources, list) or not sources:
            raise AerError(
                "INVALID_SPEC",
                "pdf.merge inputs must be a non-empty array.",
                "recipe.validate",
                target,
            )
    return arguments


def _validate_recipe(recipe: dict[str, Any], target: str) -> dict[str, Any]:
    if recipe.get("version") != 1:
        raise AerError(
            "INVALID_SPEC",
            "Only recipe version 1 is supported.",
            "recipe.validate",
            f"{target}#/version",
        )
    name = recipe.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
        raise AerError(
            "INVALID_SPEC",
            "Recipe name must use lowercase letters, digits, and hyphens.",
            "recipe.validate",
            f"{target}#/name",
        )
    inputs = recipe.get("inputs", {})
    if not isinstance(inputs, dict):
        raise AerError(
            "INVALID_SPEC",
            "Recipe inputs must be an object.",
            "recipe.validate",
            f"{target}#/inputs",
        )
    for input_name, definition in inputs.items():
        if not isinstance(input_name, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_-]{0,63}", input_name
        ):
            raise AerError(
                "INVALID_SPEC",
                "Recipe input name is invalid.",
                "recipe.validate",
                f"{target}#/inputs/{input_name}",
            )
        if not isinstance(definition, dict) or definition.get("type", "string") not in {
            "string",
            "path",
            "integer",
            "boolean",
        }:
            raise AerError(
                "INVALID_SPEC",
                "Recipe input definition is invalid.",
                "recipe.validate",
                f"{target}#/inputs/{input_name}",
            )
    steps = recipe.get("steps")
    if not isinstance(steps, list) or not steps:
        raise AerError(
            "INVALID_SPEC",
            "Recipe steps must be a non-empty array.",
            "recipe.validate",
            f"{target}#/steps",
        )
    from aer.limits import MAX_RECIPE_STEPS

    if len(steps) > MAX_RECIPE_STEPS:
        raise AerError("LIMIT_EXCEEDED", "Recipe has too many steps.", "recipe.validate", target)
    ids: set[str] = set()
    graph: dict[str, set[str]] = {}
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise AerError(
                "INVALID_SPEC",
                "Recipe step must be an object.",
                "recipe.validate",
                f"{target}#/steps/{index}",
            )
        step_id, capability = step.get("id"), step.get("uses")
        if not isinstance(step_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", step_id):
            raise AerError(
                "INVALID_SPEC",
                "Recipe step ID is invalid.",
                "recipe.validate",
                f"{target}#/steps/{index}/id",
            )
        if step_id in ids:
            raise AerError(
                "INVALID_SPEC", "Recipe step IDs must be unique.", "recipe.validate", step_id
            )
        if capability not in ALLOWED_CAPABILITIES:
            raise AerError(
                "INVALID_SPEC",
                "Recipe step capability is not allowed.",
                "recipe.validate",
                str(capability),
            )
        arguments = _validate_step_arguments(
            str(capability),
            step.get("with", {}),
            declared_inputs=set(inputs),
            target=f"{target}#/steps/{index}/with",
        )
        ids.add(step_id)
        graph[step_id] = _references(arguments)
    for step_id, dependencies in graph.items():
        unknown = dependencies - ids
        if unknown:
            raise AerError(
                "INVALID_SPEC",
                "Recipe references an unknown step.",
                "recipe.validate",
                step_id,
                {"unknown": sorted(unknown)},
            )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise AerError(
                "INVALID_SPEC", "Recipe step dependency cycle detected.", "recipe.validate", node
            )
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for step_id in graph:
        visit(step_id)
    available: set[str] = set()
    for step in steps:
        step_id = str(step["id"])
        forward = graph[step_id] - available
        if forward:
            raise AerError(
                "INVALID_SPEC",
                "Recipe steps may reference only previously completed steps.",
                "recipe.validate",
                step_id,
                {"forward": sorted(forward)},
            )
        available.add(step_id)
    return {
        "name": name,
        "input_count": len(inputs),
        "step_count": len(steps),
        "capabilities": [step["uses"] for step in steps],
    }


def _input_value(definition: dict[str, Any], raw: Any, name: str) -> Any:
    kind = definition.get("type", "string")
    if raw is None:
        if "default" in definition:
            raw = definition["default"]
        elif definition.get("required", True):
            raise AerError(
                "INVALID_ARGUMENT", "Required recipe input is missing.", "recipe.run", name
            )
        else:
            return None
    if kind in {"string", "path"}:
        return str(raw)
    if kind == "integer":
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise AerError(
                "INVALID_ARGUMENT", "Recipe input must be an integer.", "recipe.run", name
            ) from exc
    if kind == "boolean":
        if isinstance(raw, bool):
            return raw
        value = str(raw).lower()
        if value in {"true", "1", "yes"}:
            return True
        if value in {"false", "0", "no"}:
            return False
        raise AerError("INVALID_ARGUMENT", "Recipe input must be a boolean.", "recipe.run", name)
    raise AerError("INVALID_SPEC", "Unsupported recipe input type.", "recipe.run", name)


def _resolve_expression(
    match: re.Match[str], inputs: dict[str, Any], steps: dict[str, dict[str, Any]]
) -> Any:
    namespace, name, field = match.groups()
    if namespace == "inputs":
        if field is not None or name not in inputs:
            raise AerError(
                "INVALID_SPEC", "Recipe input expression is invalid.", "recipe.run", match.group(0)
            )
        return inputs[name]
    if name not in steps or field is None or field not in steps[name]:
        raise AerError(
            "INVALID_SPEC",
            "Recipe step output expression is invalid.",
            "recipe.run",
            match.group(0),
        )
    return steps[name][field]


def _substitute(value: Any, inputs: dict[str, Any], steps: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, str):
        exact = EXPRESSION.fullmatch(value)
        if exact:
            return _resolve_expression(exact, inputs, steps)

        def replace(match: re.Match[str]) -> str:
            return str(_resolve_expression(match, inputs, steps))

        return EXPRESSION.sub(replace, value)
    if isinstance(value, list):
        return [_substitute(item, inputs, steps) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, inputs, steps) for key, item in value.items()}
    return value


@contextmanager
def _step_deadline(seconds: float, step_id: str) -> Iterator[None]:
    """Interrupt an over-time capability on the primary POSIX CLI thread."""

    if (
        seconds <= 0
        or not hasattr(signal, "SIGALRM")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def timeout_handler(_signum: int, _frame: Any) -> None:
        raise AerError(
            "COMMAND_TIMEOUT",
            "Recipe step exceeded the overall timeout.",
            "recipe.run",
            step_id,
            {"timed_out": True},
        )

    signal.signal(signal.SIGALRM, timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


class RecipeEngine:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.load()
        self.settings.ensure()
        self.store = ObjectStore(self.settings)
        self.cache = ContentHashCache(self.settings)

    @staticmethod
    def _builtin_names() -> list[str]:
        directory = resources.files("aer").joinpath("resources/recipes")
        return sorted(
            item.name.removesuffix(".yaml")
            for item in directory.iterdir()
            if item.name.endswith(".yaml")
        )

    def list(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for name in self._builtin_names():
            recipe, _ = self._load(name)
            records.append(
                {
                    "name": name,
                    "description": recipe.get("description", ""),
                    "trusted": True,
                    "builtin": True,
                }
            )
        for path in sorted(self.settings.recipes_dir.glob("*.yaml")):
            if path.stem in {record["name"] for record in records}:
                continue
            try:
                recipe = _read_recipe_path(path)
                _validate_installed_name(recipe, path)
                _validate_recipe(recipe, str(path))
            except (OSError, AerError):
                continue
            records.append(
                {
                    "name": recipe["name"],
                    "description": recipe.get("description", ""),
                    "trusted": not path.is_symlink(),
                    "builtin": False,
                }
            )
        return records

    def _load(self, name_or_path: str | Path) -> tuple[dict[str, Any], bool]:
        value = str(name_or_path)
        candidate = Path(value).expanduser()
        if candidate.is_file():
            recipe = _read_recipe_path(candidate)
            if candidate.resolve().parent == self.settings.recipes_dir.resolve():
                _validate_installed_name(recipe, candidate)
            trusted = (
                not candidate.is_symlink()
                and candidate.resolve().parent == self.settings.recipes_dir.resolve()
            )
            return recipe, trusted
        if "/" in value or "\\" in value or value.endswith((".yaml", ".yml")):
            raise AerError("NOT_FOUND", "Recipe file does not exist.", "recipe", value)
        if value in self._builtin_names():
            text = (
                resources.files("aer")
                .joinpath("resources/recipes", f"{value}.yaml")
                .read_text(encoding="utf-8")
            )
            recipe = _read_yaml(text, value)
            if recipe.get("name") != value:
                raise AerError(
                    "INVALID_SPEC",
                    "Built-in recipe filename must match its declared name.",
                    "recipe.validate",
                    value,
                )
            return recipe, True
        user_path = self.settings.recipes_dir / f"{value}.yaml"
        if user_path.is_file():
            recipe = _read_recipe_path(user_path)
            if not user_path.is_symlink():
                _validate_installed_name(recipe, user_path)
            trusted = (
                not user_path.is_symlink()
                and user_path.resolve().parent == self.settings.recipes_dir.resolve()
            )
            return recipe, trusted
        raise AerError("NOT_FOUND", "Recipe was not found.", "recipe", value)

    def show(self, name: str) -> dict[str, Any]:
        recipe, trusted = self._load(name)
        summary = _validate_recipe(recipe, name)
        return {"recipe": recipe, "summary": summary, "trusted": trusted}

    def validate(self, path: Path) -> dict[str, Any]:
        recipe, trusted = self._load(path)
        return {"valid": True, "summary": _validate_recipe(recipe, str(path)), "trusted": trusted}

    def _dispatch(
        self, capability: str, arguments: dict[str, Any], *, allow_raw_command: bool, timeout: float
    ) -> dict[str, Any]:
        if capability in {
            "artifact.build",
            "presentation.build",
            "document.build",
            "workbook.build",
            "chart.build",
        }:
            result = build_artifact(
                Path(str(arguments["spec"])),
                Path(str(arguments["output"])),
                dry_run=bool(arguments.get("dry_run", False)),
            )
        elif capability == "artifact.patch":
            result = apply_patch(
                Path(str(arguments["target"])),
                Path(str(arguments["spec"])),
                dry_run=bool(arguments.get("dry_run", False)),
                backup=bool(arguments.get("backup", False)),
                expected_sha256=arguments.get("expected_sha256"),
                validate=bool(arguments.get("validate", False)),
            )
        elif capability == "artifact.validate":
            result = validate_file(
                Path(str(arguments["target"])),
                strict=bool(arguments.get("strict", False)),
                render=bool(arguments.get("render", False)),
            )
        elif capability == "archive.create":
            result = create_archive(
                Path(str(arguments["source"])),
                Path(str(arguments["output"])),
                excludes=list(arguments.get("exclude", [])),
                dry_run=bool(arguments.get("dry_run", False)),
            )
        elif capability == "archive.verify":
            result = verify_archive(Path(str(arguments["target"])))
        elif capability == "data.query":
            query = query_data(
                arguments["source"],
                sheet=arguments.get("sheet"),
                where=arguments.get("where", ()),
                select=arguments.get("select"),
                rename=arguments.get("rename"),
                sort=arguments.get("sort"),
                descending=bool(arguments.get("descending", False)),
                limit=arguments.get("limit"),
                offset=int(arguments.get("offset", 0)),
                unique=arguments.get("unique", False),
                group_by=arguments.get("group_by"),
                aggregates=arguments.get("aggregates", ()),
                duplicate_columns=arguments.get("duplicate_columns"),
                output=arguments.get("output"),
                store=self.store,
            )
            result = dict(query.to_dict())
        elif capability == "artifact.convert":
            result = convert_file(Path(str(arguments["source"])), Path(str(arguments["output"])))
        elif capability == "image.resize":
            result = resize_image(
                Path(str(arguments["source"])),
                Path(str(arguments["output"])),
                width=arguments.get("width"),
                height=arguments.get("height"),
                strip_metadata=bool(arguments.get("strip_metadata", False)),
                overwrite=bool(arguments.get("overwrite", False)),
            )
        elif capability == "image.crop":
            result = crop_image(
                Path(str(arguments["source"])),
                Path(str(arguments["output"])),
                x=int(arguments["x"]),
                y=int(arguments["y"]),
                width=int(arguments["width"]),
                height=int(arguments["height"]),
                strip_metadata=bool(arguments.get("strip_metadata", False)),
                overwrite=bool(arguments.get("overwrite", False)),
            )
        elif capability == "image.fit":
            result = fit_image(
                Path(str(arguments["source"])),
                Path(str(arguments["output"])),
                ratio=str(arguments["ratio"]),
                mode=str(arguments.get("mode", "cover")),
                width=arguments.get("width"),
                overwrite=bool(arguments.get("overwrite", False)),
            )
        elif capability == "pdf.merge":
            result = merge_pdfs(
                [Path(str(value)) for value in arguments["inputs"]], Path(str(arguments["output"]))
            )
        elif capability == "pdf.extract":
            result = extract_pdf(
                Path(str(arguments["source"])),
                Path(str(arguments["output"])),
                pages=str(arguments["pages"]),
            )
        elif capability == "pdf.split":
            result = split_pdf(Path(str(arguments["source"])), Path(str(arguments["output_dir"])))
        elif capability == "store.put":
            result = self.store.put_file(
                Path(str(arguments["source"])), pin=bool(arguments.get("pin", False))
            ).as_dict()
        elif capability == "command.run":
            if not allow_raw_command:
                raise AerError(
                    "UNTRUSTED_RECIPE",
                    "Raw command recipe steps require --allow-raw-command.",
                    "recipe.run",
                )
            command = arguments.get("argv")
            if not isinstance(command, list):
                raise AerError("INVALID_SPEC", "command.run argv must be an array.", "recipe.run")
            result = dict(
                run_command(
                    command,
                    cwd=arguments.get("cwd"),
                    timeout=min(float(arguments.get("timeout", timeout)), timeout),
                    store=self.store,
                ).to_dict()
            )
            if not result.get("ok"):
                timed_out = bool(result.get("timed_out"))
                raise AerError(
                    "COMMAND_TIMEOUT" if timed_out else "COMMAND_FAILED",
                    "Recipe command step timed out."
                    if timed_out
                    else "Recipe command step failed.",
                    "recipe.run",
                    details=result,
                    raw_ref=str(result["raw_ref"]) if result.get("raw_ref") else None,
                )
        else:
            raise AerError(
                "INVALID_SPEC", "Unsupported recipe capability.", "recipe.run", capability
            )
        return result

    @staticmethod
    def _cache_inputs(recipe: dict[str, Any], inputs: dict[str, Any]) -> str:
        destination_keys = {"output", "output_dir", "out_dir", "destination"}

        def is_content_input(input_name: str) -> bool:
            token = f"inputs.{input_name}"

            def visit(value: Any, parent_key: str | None = None) -> bool:
                if isinstance(value, str):
                    return token in value and parent_key not in destination_keys
                if isinstance(value, list):
                    return any(visit(item, parent_key) for item in value)
                if isinstance(value, dict):
                    return any(visit(item, str(key)) for key, item in value.items())
                return False

            return any(visit(step.get("with", {})) for step in recipe.get("steps", []))

        values: dict[str, Any] = {}
        for name, value in inputs.items():
            path = Path(str(value)).expanduser()
            if is_content_input(name) and path.is_symlink():
                values[name] = {
                    "path": str(path.absolute()),
                    "symlink": True,
                    "target": str(path.readlink()),
                }
            elif is_content_input(name) and path.is_file():
                values[name] = {"path": str(path.resolve()), "sha256": sha256_file(path)}
            elif is_content_input(name) and path.is_dir():
                values[name] = {
                    "path": str(path.resolve()),
                    "sha256": sha256_directory(path),
                    "type": "directory",
                }
            elif recipe.get("inputs", {}).get(name, {}).get("type") == "path":
                values[name] = {"path": str(path.resolve(strict=False))}
            else:
                values[name] = value
        return normalized_hash({"recipe": recipe, "inputs": values})

    @staticmethod
    def _cached_outputs_exist(payload: dict[str, Any]) -> bool:
        expected = payload.get("output_hashes")
        if not isinstance(expected, dict) or not expected:
            return False
        for raw_path, record in expected.items():
            if not isinstance(record, dict):
                return False
            path = Path(str(raw_path))
            if record.get("type") == "file":
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or sha256_file(path) != record.get("sha256")
                ):
                    return False
            elif record.get("type") == "directory":
                if (
                    not path.is_dir()
                    or path.is_symlink()
                    or sha256_directory(path) != record.get("sha256")
                ):
                    return False
            else:
                return False
        return True

    @staticmethod
    def _output_hashes(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
        hashes: dict[str, dict[str, str]] = {}
        for step in payload.get("steps", []):
            result = step.get("result", {})
            if not isinstance(result, dict):
                continue
            for key in ("output", "manifest", "output_dir"):
                if not result.get(key):
                    continue
                path = Path(str(result[key]))
                if path.is_file() and not path.is_symlink():
                    hashes[str(path)] = {"type": "file", "sha256": sha256_file(path)}
                elif path.is_dir() and not path.is_symlink():
                    hashes[str(path)] = {
                        "type": "directory",
                        "sha256": sha256_directory(path),
                    }
        return hashes

    def run(
        self,
        name_or_path: str | Path,
        *,
        variables: dict[str, Any],
        dry_run: bool = False,
        trust: bool = False,
        allow_raw_command: bool = False,
        timeout: float = 600,
    ) -> dict[str, Any]:
        recipe, trusted = self._load(name_or_path)
        summary = _validate_recipe(recipe, str(name_or_path))
        if not trusted and not trust:
            raise AerError(
                "UNTRUSTED_RECIPE",
                "Recipe paths outside AER_HOME require explicit --trust.",
                "recipe.run",
                str(name_or_path),
            )
        unknown = set(variables) - set(recipe.get("inputs", {}))
        if unknown:
            raise AerError(
                "INVALID_ARGUMENT",
                "Unknown recipe variables were provided.",
                "recipe.run",
                details={"unknown": sorted(unknown)},
            )
        inputs = {
            name: _input_value(definition, variables.get(name), name)
            for name, definition in recipe.get("inputs", {}).items()
        }
        if dry_run:
            return {
                "dry_run": True,
                "recipe": summary["name"],
                "inputs": inputs,
                "steps": [
                    {
                        "id": step["id"],
                        "uses": step["uses"],
                        "with": _substitute(step.get("with", {}), inputs, {})
                        if not _references(step.get("with", {}))
                        else step.get("with", {}),
                    }
                    for step in recipe["steps"]
                ],
            }
        cache_key: str | None = None
        if _recipe_cacheable(recipe):
            cache_key = self.cache.key_for(
                "recipe.run",
                "1",
                self._cache_inputs(recipe, inputs),
                spec_hash=normalized_hash(recipe),
                configuration={"allow_raw_command": allow_raw_command},
                dependency_versions={"python": platform.python_version()},
            )
            cached = self.cache.get_bytes(cache_key)
            if cached:
                payload = cast(dict[str, Any], json.loads(cached))
                if self._cached_outputs_exist(payload):
                    payload["cache_hit"] = True
                    return payload
        started = time.monotonic()
        step_results: dict[str, dict[str, Any]] = {}
        log: list[dict[str, Any]] = []
        for step in recipe["steps"]:
            elapsed = time.monotonic() - started
            if elapsed >= timeout:
                raise AerError(
                    "COMMAND_TIMEOUT",
                    "Recipe timeout was exceeded.",
                    "recipe.run",
                    step["id"],
                    {"timed_out": True},
                )
            arguments = _substitute(step.get("with", {}), inputs, step_results)
            step_started = time.monotonic()
            try:
                with _step_deadline(timeout - elapsed, str(step["id"])):
                    result = self._dispatch(
                        step["uses"],
                        arguments,
                        allow_raw_command=allow_raw_command and (trusted or trust),
                        timeout=timeout - elapsed,
                    )
                if time.monotonic() - started >= timeout:
                    raise AerError(
                        "COMMAND_TIMEOUT",
                        "Recipe step exceeded the overall timeout.",
                        "recipe.run",
                        step["id"],
                        {"timed_out": True},
                    )
            except AerError as exc:
                log.append(
                    {
                        "id": step["id"],
                        "uses": step["uses"],
                        "ok": False,
                        "code": exc.code,
                        "message": exc.message,
                        "duration_ms": round((time.monotonic() - step_started) * 1000),
                    }
                )
                recipe_log_ref = self._store_log(str(recipe["name"]), log)
                exc.details = {
                    **exc.details,
                    "failed_step": step["id"],
                    "recipe_log_ref": recipe_log_ref,
                }
                if exc.raw_ref is None:
                    exc.raw_ref = recipe_log_ref
                raise
            result.setdefault("duration_ms", round((time.monotonic() - step_started) * 1000))
            step_results[step["id"]] = result
            log.append({"id": step["id"], "uses": step["uses"], "ok": True, "result": result})
        log_ref = self._store_log(str(recipe["name"]), log)
        payload = {
            "recipe": recipe["name"],
            "success": True,
            "steps": log,
            "log_ref": log_ref,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "cache_hit": False,
        }
        payload["output_hashes"] = self._output_hashes(payload)
        if cache_key is not None:
            self.cache.put(
                cache_key,
                json.dumps(payload, ensure_ascii=False, default=str).encode(),
                filename=f"recipe-{recipe['name']}-result.json",
                mime_type="application/json",
            )
        return payload

    def _store_log(self, recipe_name: str, log: Sequence[dict[str, Any]]) -> str:
        return self.store.put_bytes(
            (
                "\n".join(json.dumps(item, ensure_ascii=False, default=str) for item in log) + "\n"
            ).encode(),
            filename=f"recipe-{recipe_name}.jsonl",
            mime_type="application/x-ndjson",
            source={"recipe": recipe_name},
        ).ref
