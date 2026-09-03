from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import click
import typer

try:
    from typer._click.exceptions import ClickException as BundledClickException
except ImportError:  # Typer versions that use external Click
    BundledClickException = click.ClickException  # type: ignore[misc,assignment]

from aer import __version__
from aer.archive import create_archive, list_archive, verify_archive
from aer.artifacts import build_artifact
from aer.conversion import convert_file
from aer.data import data_response, query_data
from aer.errors import AerError
from aer.image import batch_images, crop_image, fit_image, inspect_image, resize_image
from aer.inspect import inspect_target
from aer.patch import apply_patch
from aer.paths import atomic_write_text
from aer.pdf import extract_pdf, inspect_pdf, merge_pdfs, split_pdf
from aer.protocol import execute, failure, success
from aer.recipes import RecipeEngine
from aer.registry import discover as discover_capabilities
from aer.registry import list_names
from aer.registry import schema as capability_schema
from aer.runner import command_response, run_command
from aer.state import StateManager
from aer.store import ObjectStore
from aer.validation import validate_file


@dataclass(slots=True)
class OutputOptions:
    pretty: bool = False
    human: bool = False
    debug: bool = False
    full: bool = False


CONTEXT = {"help_option_names": ["-h", "--help"]}
app = typer.Typer(
    name="aer",
    help="Agent Efficiency Runtime",
    add_completion=False,
    context_settings=CONTEXT,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
store_app = typer.Typer(help="Content-addressed object storage.", rich_markup_mode=None)
data_app = typer.Typer(help="Local tabular data operations.", rich_markup_mode=None)
image_app = typer.Typer(help="Deterministic image operations.", rich_markup_mode=None)
pdf_app = typer.Typer(help="PDF operations.", rich_markup_mode=None)
archive_app = typer.Typer(help="Deterministic ZIP operations.", rich_markup_mode=None)
state_app = typer.Typer(help="Persistent task state.", rich_markup_mode=None)
recipe_app = typer.Typer(help="Trusted deterministic workflows.", rich_markup_mode=None)
profile_app = typer.Typer(help="Measured agent-work profiles.", rich_markup_mode=None)
benchmark_app = typer.Typer(help="Token-efficiency benchmarks.", rich_markup_mode=None)

app.add_typer(store_app, name="store")
app.add_typer(data_app, name="data")
app.add_typer(image_app, name="image")
app.add_typer(pdf_app, name="pdf")
app.add_typer(archive_app, name="archive")
app.add_typer(state_app, name="state")
app.add_typer(recipe_app, name="recipe")
app.add_typer(profile_app, name="profile")
app.add_typer(benchmark_app, name="benchmark")


def _options(context: typer.Context) -> OutputOptions:
    value = context.find_root().obj
    return value if isinstance(value, OutputOptions) else OutputOptions()


def _domain_response(operation: str, result: dict[str, Any]) -> dict[str, Any]:
    if "ok" in result:
        return result
    copied = dict(result)
    warnings = copied.pop("warnings", [])
    artifacts: list[dict[str, Any]] = []
    if copied.get("output"):
        artifacts.append({"path": copied["output"], "role": "output"})
    for key, role in (("raw_ref", "raw"), ("result_ref", "result"), ("log_ref", "log")):
        if copied.get(key):
            artifacts.append({"ref": copied[key], "role": role})
    return success(operation, copied, artifacts=artifacts, warnings=warnings)


def _respond(
    context: typer.Context,
    operation: str,
    function: Callable[[], dict[str, Any]],
    *,
    full: bool = False,
) -> None:
    options = _options(context)
    code = execute(
        operation,
        lambda: _domain_response(operation, function()),
        pretty=options.pretty,
        human=options.human,
        debug=options.debug,
        full=full or options.full,
    )
    if code:
        raise typer.Exit(code)


def _pairs(values: list[str], *, operation: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise AerError("INVALID_ARGUMENT", "Expected key=value.", operation, value)
        key, item = value.split("=", 1)
        if not key:
            raise AerError("INVALID_ARGUMENT", "Key cannot be empty.", operation, value)
        result[key] = item
    return result


@app.callback(invoke_without_command=True)
def root(
    context: typer.Context,
    pretty: bool = typer.Option(False, "--pretty", help="Indent JSON output."),
    human: bool = typer.Option(False, "--human", help="Use a short human-readable line."),
    debug: bool = typer.Option(False, "--debug", help="Write internal tracebacks to stderr."),
    full: bool = typer.Option(
        False, "--full", help="Permit explicitly requested full text output."
    ),
    version: bool = typer.Option(False, "--version", help="Show the installed version."),
) -> None:
    context.obj = OutputOptions(pretty=pretty, human=human, debug=debug, full=full)
    if version:
        _respond(context, "version", lambda: {"version": __version__})
    if context.invoked_subcommand is None and not version:
        raise AerError(
            "INVALID_ARGUMENT",
            "A command is required.",
            "cli",
            suggested_action="Use `aer --help`.",
        )


@app.command("discover")
def discover_command(
    context: typer.Context,
    query: str = typer.Argument(...),
    limit: int = typer.Option(5, "--limit", min=1, max=20),
) -> None:
    _respond(context, "discover", lambda: discover_capabilities(query, limit=limit))


@app.command("schema")
def schema_command(
    context: typer.Context,
    name: str | None = typer.Argument(None),
    compact: bool = typer.Option(False, "--compact"),
    example: bool = typer.Option(False, "--example"),
    list_all: bool = typer.Option(False, "--list-names"),
) -> None:
    def action() -> dict[str, Any]:
        if list_all:
            return list_names()
        if name is None:
            raise AerError(
                "INVALID_ARGUMENT",
                "Capability name is required.",
                "schema",
                suggested_action="Use `aer schema --list-names`.",
            )
        return dict(capability_schema(name, compact=compact, example=example))

    _respond(context, "schema", action)


@store_app.command("put")
def store_put(
    context: typer.Context,
    file: Path | None = typer.Argument(None),
    stdin: bool = typer.Option(False, "--stdin"),
    filename: str | None = typer.Option(None, "--filename"),
    mime_type: str | None = typer.Option(None, "--mime-type"),
    pin: bool = typer.Option(False, "--pin"),
) -> None:
    def action() -> dict[str, Any]:
        store = ObjectStore()
        if stdin:
            if file is not None:
                raise AerError(
                    "INVALID_ARGUMENT", "FILE and --stdin are mutually exclusive.", "store.put"
                )
            return store.put_stdin(filename=filename, mime_type=mime_type, pin=pin).as_dict()
        if file is None:
            raise AerError("INVALID_ARGUMENT", "FILE or --stdin is required.", "store.put")
        return store.put_file(file, filename=filename, mime_type=mime_type, pin=pin).as_dict()

    _respond(context, "store.put", action)


@store_app.command("get")
def store_get(
    context: typer.Context,
    ref: str = typer.Argument(...),
    output: Path = typer.Option(..., "-o", "--output"),
    overwrite: bool = typer.Option(False, "--overwrite"),
) -> None:
    _respond(
        context,
        "store.get",
        lambda: (
            ObjectStore().get(ref, output, overwrite=overwrite).as_dict()
            | {"output": str(output.resolve())}
        ),
    )


@store_app.command("cat")
def store_cat(
    context: typer.Context,
    ref: str = typer.Argument(...),
    start_line: int | None = typer.Option(None, "--start-line", min=1),
    end_line: int | None = typer.Option(None, "--end-line", min=1),
    encoding: str = typer.Option("utf-8", "--encoding"),
    full: bool = typer.Option(False, "--full"),
) -> None:
    options = _options(context)
    _respond(
        context,
        "store.cat",
        lambda: (
            ObjectStore()
            .cat(
                ref,
                start_line=start_line,
                end_line=end_line,
                encoding=encoding,
                full=full or options.full,
            )
            .as_dict()
        ),
        full=full,
    )


@store_app.command("stat")
def store_stat(context: typer.Context, ref: str = typer.Argument(...)) -> None:
    _respond(context, "store.stat", lambda: ObjectStore().stat(ref).as_dict())


@store_app.command("verify")
def store_verify(context: typer.Context, ref: str = typer.Argument(...)) -> None:
    _respond(
        context, "store.verify", lambda: ObjectStore().verify(ref).as_dict() | {"verified": True}
    )


@store_app.command("list")
def store_list(
    context: typer.Context,
    limit: int = typer.Option(20, "--limit", min=1, max=1000),
    offset: int = typer.Option(0, "--offset", min=0),
) -> None:
    _respond(
        context,
        "store.list",
        lambda: {
            "objects": [
                record.as_dict() for record in ObjectStore().list(limit=limit, offset=offset)
            ]
        },
    )


def _age(value: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([dhm])", value)
    if not match:
        raise AerError(
            "INVALID_ARGUMENT", "Age must look like 30d, 12h, or 15m.", "store.gc", value
        )
    amount = int(match.group(1))
    return {
        "d": timedelta(days=amount),
        "h": timedelta(hours=amount),
        "m": timedelta(minutes=amount),
    }[match.group(2)]


@store_app.command("gc")
def store_gc(
    context: typer.Context,
    older_than: str = typer.Option(..., "--older-than"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    _respond(
        context,
        "store.gc",
        lambda: ObjectStore().gc(older_than=_age(older_than), dry_run=dry_run).as_dict(),
    )


@store_app.command("pin")
def store_pin(context: typer.Context, ref: str = typer.Argument(...)) -> None:
    _respond(context, "store.pin", lambda: ObjectStore().pin(ref).as_dict())


@store_app.command("unpin")
def store_unpin(context: typer.Context, ref: str = typer.Argument(...)) -> None:
    _respond(context, "store.unpin", lambda: ObjectStore().pin(ref, pinned=False).as_dict())


@app.command("inspect")
def inspect_command(
    context: typer.Context,
    target: str = typer.Argument(...),
    summary: bool = typer.Option(False, "--summary"),
    outline: bool = typer.Option(False, "--outline"),
    selector: str | None = typer.Option(None, "--selector"),
    query: str | None = typer.Option(None, "--query"),
    regex: bool = typer.Option(False, "--regex"),
    case_sensitive: bool = typer.Option(False, "--case-sensitive"),
    context_lines: int = typer.Option(0, "--context", min=0, max=20),
    max_items: int = typer.Option(20, "--max-items", min=1),
    start_line: int | None = typer.Option(None, "--start-line", min=1),
    end_line: int | None = typer.Option(None, "--end-line", min=1),
    sheet: str | None = typer.Option(None, "--sheet"),
    cell_range: str | None = typer.Option(None, "--range"),
    formulas: bool = typer.Option(False, "--formulas"),
    rows: str | None = typer.Option(None, "--rows"),
    slide: int | None = typer.Option(None, "--slide", min=1),
    page: int | None = typer.Option(None, "--page", min=1),
    glob_pattern: str | None = typer.Option(None, "--glob"),
    changed: bool = typer.Option(False, "--changed"),
    max_depth: int = typer.Option(6, "--max-depth", min=1, max=20),
    full: bool = typer.Option(False, "--full"),
) -> None:
    def action() -> dict[str, Any]:
        store = ObjectStore()
        temporary_paths: list[Path] = []

        def resolver(ref: str) -> Path:
            path = store.materialize_path(ref)
            if path.name.startswith(".resolved-"):
                temporary_paths.append(path)
            return path

        try:
            return inspect_target(
                target,
                summary=summary,
                outline=outline,
                selector=selector,
                query=query,
                regex=regex,
                case_sensitive=case_sensitive,
                context=context_lines,
                max_items=max_items,
                start_line=start_line,
                end_line=end_line,
                sheet=sheet,
                cell_range=cell_range,
                formulas=formulas,
                rows=rows,
                slide=slide,
                page=page,
                glob=glob_pattern,
                changed=changed,
                max_depth=max_depth,
                full=full or _options(context).full,
                resolver=resolver,
                raw_sink=lambda data, name: store.put_bytes(data, filename=name).ref,
            )
        finally:
            for path in temporary_paths:
                path.unlink(missing_ok=True)

    _respond(context, "inspect", action, full=full)


@app.command(
    "run",
    context_settings={
        "allow_extra_args": True,
        "ignore_unknown_options": True,
        "help_option_names": ["-h", "--help"],
    },
)
def run_command_cli(
    context: typer.Context,
    timeout: float = typer.Option(300, "--timeout", min=0.001),
    cwd: Path | None = typer.Option(None, "--cwd"),
) -> None:
    argv = list(context.args)
    if argv and argv[0] == "--":
        argv = argv[1:]
    _respond(
        context,
        "command.run",
        lambda: command_response(run_command(argv, cwd=cwd, timeout=timeout, store=ObjectStore())),
    )


@data_app.command("query")
def data_query(
    context: typer.Context,
    source: Path = typer.Argument(...),
    sheet: str | None = typer.Option(None, "--sheet"),
    where: list[str] | None = typer.Option(None, "--where"),
    select: str | None = typer.Option(None, "--select"),
    rename: list[str] | None = typer.Option(None, "--rename"),
    sort: str | None = typer.Option(None, "--sort"),
    descending: bool = typer.Option(False, "--descending"),
    limit: int | None = typer.Option(None, "--limit", min=0),
    offset: int = typer.Option(0, "--offset", min=0),
    unique: bool = typer.Option(False, "--unique"),
    unique_columns: str | None = typer.Option(None, "--unique-columns"),
    group_by: str | None = typer.Option(None, "--group-by"),
    aggregate: list[str] | None = typer.Option(None, "--aggregate"),
    duplicates: str | None = typer.Option(None, "--duplicates"),
    output: Path | None = typer.Option(None, "-o", "--output"),
) -> None:
    def action() -> dict[str, Any]:
        rename_map = _pairs(rename or [], operation="data.query") if rename else None
        unique_value: bool | str = unique_columns if unique_columns is not None else unique
        result = query_data(
            source,
            sheet=sheet,
            where=where or (),
            select=select,
            rename=rename_map,
            sort=sort,
            descending=descending,
            limit=limit,
            offset=offset,
            unique=unique_value,
            group_by=group_by,
            aggregates=aggregate or (),
            duplicate_columns=duplicates,
            output=output,
            store=ObjectStore(),
        )
        return data_response(result)

    _respond(context, "data.query", action)


@app.command("build")
def build_command(
    context: typer.Context,
    spec: Path = typer.Argument(...),
    output: Path | None = typer.Option(None, "-o", "--output"),
    validate: bool = typer.Option(False, "--validate"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    def action() -> dict[str, Any]:
        result = build_artifact(spec, output, dry_run=dry_run)
        if validate and not dry_run and output is not None:
            result["validation"] = validate_file(output)
        operation = str(result.pop("operation", "artifact.build"))
        return _domain_response(operation, result)

    _respond(context, "artifact.build", action)


@app.command("patch")
def patch_command(
    context: typer.Context,
    target: Path = typer.Argument(...),
    spec: Path = typer.Option(..., "--spec"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    backup: bool = typer.Option(False, "--backup"),
    expected_sha256: str | None = typer.Option(None, "--expected-sha256"),
    validate: bool = typer.Option(False, "--validate"),
) -> None:
    def action() -> dict[str, Any]:
        return apply_patch(
            target,
            spec,
            dry_run=dry_run,
            backup=backup,
            expected_sha256=expected_sha256,
            validate=validate,
        )

    _respond(context, "artifact.patch", action)


@app.command("validate")
def validate_command(
    context: typer.Context,
    file: Path = typer.Argument(...),
    render: bool = typer.Option(False, "--render"),
    strict: bool = typer.Option(False, "--strict"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    del json_output
    _respond(
        context, "artifact.validate", lambda: validate_file(file, strict=strict, render=render)
    )


@app.command("convert")
def convert_command(
    context: typer.Context,
    source: Path = typer.Argument(...),
    output: Path = typer.Option(..., "-o", "--output"),
) -> None:
    _respond(context, "artifact.convert", lambda: convert_file(source, output))


@image_app.command("inspect")
def image_inspect(context: typer.Context, source: Path = typer.Argument(...)) -> None:
    _respond(context, "image.inspect", lambda: inspect_image(source))


@image_app.command("resize")
def image_resize(
    context: typer.Context,
    source: Path = typer.Argument(...),
    output: Path = typer.Option(..., "-o", "--output"),
    width: int | None = typer.Option(None, "--width", min=1),
    height: int | None = typer.Option(None, "--height", min=1),
    strip_metadata: bool = typer.Option(False, "--strip-metadata"),
    overwrite: bool = typer.Option(False, "--overwrite"),
) -> None:
    _respond(
        context,
        "image.resize",
        lambda: resize_image(
            source,
            output,
            width=width,
            height=height,
            strip_metadata=strip_metadata,
            overwrite=overwrite,
        ),
    )


@image_app.command("crop")
def image_crop(
    context: typer.Context,
    source: Path = typer.Argument(...),
    output: Path = typer.Option(..., "-o", "--output"),
    x: int = typer.Option(..., "--x", min=0),
    y: int = typer.Option(..., "--y", min=0),
    width: int = typer.Option(..., "--width", min=1),
    height: int = typer.Option(..., "--height", min=1),
    strip_metadata: bool = typer.Option(False, "--strip-metadata"),
    overwrite: bool = typer.Option(False, "--overwrite"),
) -> None:
    _respond(
        context,
        "image.crop",
        lambda: crop_image(
            source,
            output,
            x=x,
            y=y,
            width=width,
            height=height,
            strip_metadata=strip_metadata,
            overwrite=overwrite,
        ),
    )


@image_app.command("fit")
def image_fit(
    context: typer.Context,
    source: Path = typer.Argument(...),
    output: Path = typer.Option(..., "-o", "--output"),
    ratio: str = typer.Option(..., "--ratio"),
    mode: str = typer.Option("cover", "--mode"),
    width: int | None = typer.Option(None, "--width", min=1),
    background: str = typer.Option("white", "--background"),
    strip_metadata: bool = typer.Option(False, "--strip-metadata"),
    overwrite: bool = typer.Option(False, "--overwrite"),
) -> None:
    _respond(
        context,
        "image.fit",
        lambda: fit_image(
            source,
            output,
            ratio=ratio,
            mode=mode,
            width=width,
            background=background,
            strip_metadata=strip_metadata,
            overwrite=overwrite,
        ),
    )


@image_app.command("batch")
def image_batch(
    context: typer.Context,
    pattern: str = typer.Argument(...),
    out_dir: Path = typer.Option(..., "--out-dir"),
    width: int = typer.Option(..., "--width", min=1),
    strip_metadata: bool = typer.Option(False, "--strip-metadata"),
    overwrite: bool = typer.Option(False, "--overwrite"),
) -> None:
    _respond(
        context,
        "image.batch",
        lambda: batch_images(
            pattern, out_dir, width=width, strip_metadata=strip_metadata, overwrite=overwrite
        ),
    )


@pdf_app.command("merge")
def pdf_merge(
    context: typer.Context,
    inputs: list[Path] = typer.Argument(...),
    output: Path = typer.Option(..., "-o", "--output"),
) -> None:
    _respond(context, "pdf.merge", lambda: merge_pdfs(inputs, output))


@pdf_app.command("extract")
def pdf_extract(
    context: typer.Context,
    source: Path = typer.Argument(...),
    pages: str = typer.Option(..., "--pages"),
    output: Path = typer.Option(..., "-o", "--output"),
) -> None:
    _respond(context, "pdf.extract", lambda: extract_pdf(source, output, pages=pages))


@pdf_app.command("split")
def pdf_split(
    context: typer.Context,
    source: Path = typer.Argument(...),
    out_dir: Path = typer.Option(..., "--out-dir"),
) -> None:
    _respond(context, "pdf.split", lambda: split_pdf(source, out_dir))


@pdf_app.command("inspect")
def pdf_inspect(
    context: typer.Context,
    source: Path = typer.Argument(...),
    page: int | None = typer.Option(None, "--page", min=1),
) -> None:
    _respond(context, "pdf.inspect", lambda: inspect_pdf(source, page=page))


@archive_app.command("create")
def archive_create(
    context: typer.Context,
    source: Path = typer.Argument(...),
    output: Path = typer.Option(..., "-o", "--output"),
    exclude: list[str] | None = typer.Option(None, "--exclude"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    _respond(
        context,
        "archive.create",
        lambda: create_archive(source, output, excludes=exclude or [], dry_run=dry_run),
    )


@archive_app.command("verify")
def archive_verify(context: typer.Context, source: Path = typer.Argument(...)) -> None:
    _respond(context, "archive.verify", lambda: verify_archive(source))


@archive_app.command("list")
def archive_list(
    context: typer.Context,
    source: Path = typer.Argument(...),
    limit: int = typer.Option(20, "--limit", min=1),
) -> None:
    _respond(context, "archive.list", lambda: list_archive(source, limit=limit))


@state_app.command("init")
def state_init(
    context: typer.Context,
    task_id: str = typer.Argument(...),
    goal: str = typer.Option(..., "--goal"),
) -> None:
    _respond(context, "state.init", lambda: StateManager().init(task_id, goal))


@state_app.command("update")
def state_update(
    context: typer.Context,
    task_id: str = typer.Argument(...),
    complete: list[str] | None = typer.Option(None, "--complete"),
    remaining: list[str] | None = typer.Option(None, "--remaining"),
    decision: list[str] | None = typer.Option(None, "--decision"),
    artifact: list[str] | None = typer.Option(None, "--artifact"),
    warning: list[str] | None = typer.Option(None, "--warning"),
    status: str | None = typer.Option(None, "--status"),
) -> None:
    _respond(
        context,
        "state.update",
        lambda: StateManager().update(
            task_id,
            completed=complete or [],
            remaining=remaining or [],
            decisions=_pairs(decision or [], operation="state.update"),
            artifacts=_pairs(artifact or [], operation="state.update"),
            warnings=warning or [],
            status=status,
        ),
    )


@state_app.command("show")
def state_show(context: typer.Context, task_id: str = typer.Argument(...)) -> None:
    _respond(context, "state.show", lambda: StateManager().show(task_id))


@state_app.command("list")
def state_list(context: typer.Context, limit: int = typer.Option(20, "--limit", min=1)) -> None:
    _respond(context, "state.list", lambda: {"tasks": StateManager().list(limit=limit)})


@state_app.command("checkpoint")
def state_checkpoint(context: typer.Context, task_id: str = typer.Argument(...)) -> None:
    _respond(context, "state.checkpoint", lambda: StateManager().checkpoint(task_id))


@state_app.command("export")
def state_export(
    context: typer.Context,
    task_id: str = typer.Argument(...),
    output: Path | None = typer.Option(None, "-o", "--output"),
) -> None:
    _respond(context, "state.export", lambda: StateManager().export(task_id, output))


@recipe_app.command("list")
def recipe_list(context: typer.Context) -> None:
    _respond(context, "recipe.list", lambda: {"recipes": RecipeEngine().list()})


@recipe_app.command("show")
def recipe_show(context: typer.Context, name: str = typer.Argument(...)) -> None:
    _respond(context, "recipe.show", lambda: RecipeEngine().show(name))


@recipe_app.command("validate")
def recipe_validate(context: typer.Context, file: Path = typer.Argument(...)) -> None:
    _respond(context, "recipe.validate", lambda: RecipeEngine().validate(file))


@recipe_app.command("run")
def recipe_run(
    context: typer.Context,
    name: str = typer.Argument(...),
    variables: list[str] | None = typer.Option(None, "--var"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    trust: bool = typer.Option(False, "--trust"),
    allow_raw_command: bool = typer.Option(False, "--allow-raw-command"),
    timeout: float = typer.Option(600, "--timeout", min=0.001),
) -> None:
    _respond(
        context,
        "recipe.run",
        lambda: RecipeEngine().run(
            name,
            variables=_pairs(variables or [], operation="recipe.run"),
            dry_run=dry_run,
            trust=trust,
            allow_raw_command=allow_raw_command,
            timeout=timeout,
        ),
    )


@profile_app.command("record")
def profile_record(
    context: typer.Context,
    task: str = typer.Option(..., "--task"),
    variant: str = typer.Option(..., "--variant"),
    model: str | None = typer.Option(None, "--model"),
    model_calls: int | None = typer.Option(None, "--model-calls", min=0),
    tool_calls: int | None = typer.Option(None, "--tool-calls", min=0),
    input_tokens: int | None = typer.Option(None, "--input-tokens", min=0),
    cached_input_tokens: int | None = typer.Option(None, "--cached-input-tokens", min=0),
    output_tokens: int | None = typer.Option(None, "--output-tokens", min=0),
    reasoning_tokens: int | None = typer.Option(None, "--reasoning-tokens", min=0),
    tool_schema_tokens: int | None = typer.Option(None, "--tool-schema-tokens", min=0),
    tool_result_tokens: int | None = typer.Option(None, "--tool-result-tokens", min=0),
    retries: int | None = typer.Option(None, "--retries", min=0),
    duration_ms: int | None = typer.Option(None, "--duration-ms", min=0),
    success_value: str = typer.Option(..., "--success"),
    human_edits: int | None = typer.Option(None, "--human-edits", min=0),
    notes: str | None = typer.Option(None, "--notes"),
) -> None:
    from aer.profile import ProfileStore

    normalized = success_value.lower()
    if normalized not in {"true", "false", "1", "0", "yes", "no"}:
        raise AerError(
            "INVALID_ARGUMENT", "--success must be true or false.", "profile.record", success_value
        )
    successful = normalized in {"true", "1", "yes"}
    _respond(
        context,
        "profile.record",
        lambda: (
            ProfileStore()
            .record(
                task=task,
                variant=variant,
                model=model,
                model_calls=model_calls,
                tool_calls=tool_calls,
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                tool_schema_tokens=tool_schema_tokens,
                tool_result_tokens=tool_result_tokens,
                retries=retries,
                duration_ms=duration_ms,
                success=successful,
                human_edits=human_edits,
                notes=notes,
            )
            .as_dict()
        ),
    )


@profile_app.command("report")
def profile_report(context: typer.Context, task: str | None = typer.Option(None, "--task")) -> None:
    from aer.profile import ProfileStore

    _respond(context, "profile.report", lambda: ProfileStore().report(task=task).as_dict())


@profile_app.command("compare")
def profile_compare(context: typer.Context, task: str = typer.Option(..., "--task")) -> None:
    from aer.profile import ProfileStore

    _respond(context, "profile.compare", lambda: ProfileStore().compare(task=task).as_dict())


@profile_app.command("export")
def profile_export(
    context: typer.Context, output: Path = typer.Option(..., "-o", "--output")
) -> None:
    from aer.profile import ProfileStore

    _respond(
        context,
        "profile.export",
        lambda: {"output": str(output.resolve()), "rows": ProfileStore().export(output)},
    )


@benchmark_app.command("run")
def benchmark_run(
    context: typer.Context,
    scenario: str | None = typer.Option(None, "--scenario"),
    output: Path | None = typer.Option(None, "-o", "--output"),
) -> None:
    from aer.benchmark import BenchmarkEngine

    def run() -> dict[str, Any]:
        payload = BenchmarkEngine().run(scenario=scenario).as_dict()
        if output is not None:
            atomic_write_text(
                output,
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
            payload["output"] = str(output.resolve())
        return payload

    _respond(context, "benchmark.run", run)


@benchmark_app.command("report")
def benchmark_report(
    context: typer.Context,
    limit: int = typer.Option(10, "--limit", min=1, max=1000),
    output: Path | None = typer.Option(None, "-o", "--output"),
) -> None:
    from aer.benchmark import BenchmarkEngine

    def report() -> dict[str, Any]:
        payload: dict[str, Any] = {
            "runs": [run.as_dict() for run in BenchmarkEngine().report(limit=limit)]
        }
        if output is not None:
            atomic_write_text(
                output,
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
            payload["output"] = str(output.resolve())
        return payload

    _respond(context, "benchmark.report", report)


@app.command("doctor")
def doctor_command(context: typer.Context) -> None:
    from aer.doctor import doctor_response, run_doctor

    _respond(context, "doctor", lambda: doctor_response(run_doctor()))


def main() -> None:
    try:
        result = app(standalone_mode=False)
        if isinstance(result, int) and result:
            raise SystemExit(result)
    except typer.Exit as exc:
        raise SystemExit(exc.exit_code) from None
    except AerError as error:
        payload = failure(error)
        execute(error.operation, lambda: payload)
        raise SystemExit(error.exit_code) from None
    except (click.ClickException, BundledClickException) as exc:
        usage_error = AerError("INVALID_ARGUMENT", exc.format_message(), "cli")
        execute("cli", lambda: failure(usage_error))
        raise SystemExit(usage_error.exit_code) from None


if __name__ == "__main__":
    main()
