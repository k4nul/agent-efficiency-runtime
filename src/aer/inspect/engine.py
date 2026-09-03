"""Single bounded inspection dispatcher."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aer.errors import AerError
from aer.inspect.common import (
    RawSink,
    TargetResolver,
    enforce_output_budget,
    resolve_target,
    validate_limits,
)
from aer.inspect.office import inspect_docx, inspect_pdf, inspect_pptx, inspect_xlsx
from aer.inspect.repository import inspect_repository
from aer.inspect.structured import inspect_structured
from aer.inspect.tabular import inspect_tabular
from aer.inspect.text import inspect_text
from aer.limits import DEFAULT_MAX_ITEMS

_TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".conf",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".log",
    ".md",
    ".py",
    ".pyi",
    ".rst",
    ".sh",
    ".sql",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".lock",
    ".xml",
}


class Inspector:
    """Bind optional object-store hooks to repeated inspection calls."""

    def __init__(
        self,
        *,
        resolver: TargetResolver | None = None,
        raw_sink: RawSink | None = None,
    ) -> None:
        self.resolver = resolver
        self.raw_sink = raw_sink

    def inspect(self, target: str | Path, **options: Any) -> dict[str, Any]:
        return inspect_target(
            target,
            resolver=self.resolver,
            raw_sink=self.raw_sink,
            **options,
        )


def inspect_target(
    target: str | Path,
    *,
    summary: bool = False,
    outline: bool = False,
    selector: str | None = None,
    query: str | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
    context: int = 0,
    max_items: int = DEFAULT_MAX_ITEMS,
    start_line: int | None = None,
    end_line: int | None = None,
    sheet: str | None = None,
    cell_range: str | None = None,
    range_: str | None = None,
    formulas: bool = False,
    rows: str | None = None,
    slide: int | None = None,
    page: int | None = None,
    glob: str | None = None,
    changed: bool = False,
    max_depth: int = 6,
    full: bool = False,
    resolver: TargetResolver | None = None,
    raw_sink: RawSink | None = None,
) -> dict[str, Any]:
    """Inspect one target without returning unbounded content.

    `resolver` maps an ``aer://sha256/...`` reference to a readable local path.
    `raw_sink` accepts complete overflow bytes plus a suggested name and returns a
    durable reference. Neither hook is required for ordinary local previews.
    """

    del summary  # structural summaries are always present and compact
    effective_max = 10_000 if full and max_items == DEFAULT_MAX_ITEMS else max_items
    validate_limits(max_items=effective_max, context=context, full=full)
    if max_depth < 1 or max_depth > 20:
        raise AerError(
            "INVALID_ARGUMENT",
            "max_depth must be between 1 and 20.",
            operation="inspect",
            target=str(max_depth),
        )
    if range_ is not None:
        if cell_range is not None and cell_range != range_:
            raise AerError(
                "INVALID_SELECTOR",
                "cell_range and range_ disagree.",
                operation="inspect",
                target=range_,
            )
        cell_range = range_
    path = resolve_target(target, resolver)
    if path.is_dir():
        result = inspect_repository(
            path,
            outline=outline,
            query=query,
            glob=glob,
            changed=changed,
            regex=regex,
            case_sensitive=case_sensitive,
            context=context,
            max_items=effective_max,
            raw_sink=raw_sink,
            full=full,
        )
    else:
        suffix = path.suffix.casefold()
        if suffix == ".json":
            result = inspect_structured(
                path,
                kind="json",
                outline=outline,
                selector=selector,
                query=query,
                max_items=effective_max,
                max_depth=max_depth,
                raw_sink=raw_sink,
            )
        elif suffix in {".yaml", ".yml"}:
            result = inspect_structured(
                path,
                kind="yaml",
                outline=outline,
                selector=selector,
                query=query,
                max_items=effective_max,
                max_depth=max_depth,
                raw_sink=raw_sink,
            )
        elif suffix in {".csv", ".tsv", ".jsonl", ".ndjson"}:
            kind = "jsonl" if suffix in {".jsonl", ".ndjson"} else suffix[1:]
            result = inspect_tabular(
                path,
                kind=kind,
                selector=selector,
                query=query,
                rows=rows,
                max_items=effective_max,
                raw_sink=raw_sink,
            )
        elif suffix == ".xlsx":
            result = inspect_xlsx(
                path,
                sheet=sheet,
                cell_range=cell_range,
                rows=rows,
                selector=selector,
                formulas=formulas,
                max_items=effective_max,
                raw_sink=raw_sink,
            )
        elif suffix == ".pptx":
            result = inspect_pptx(
                path,
                slide=slide,
                selector=selector,
                query=query,
                max_items=effective_max,
                raw_sink=raw_sink,
                full=full,
            )
        elif suffix == ".docx":
            result = inspect_docx(
                path,
                outline=outline,
                selector=selector,
                query=query,
                max_items=effective_max,
                raw_sink=raw_sink,
                full=full,
            )
        elif suffix == ".pdf":
            result = inspect_pdf(
                path,
                page=page,
                selector=selector,
                query=query,
                max_items=effective_max,
                raw_sink=raw_sink,
                full=full,
            )
        elif suffix in _TEXT_SUFFIXES:
            result = inspect_text(
                path,
                query=query,
                regex=regex,
                case_sensitive=case_sensitive,
                context=context,
                start_line=start_line,
                end_line=end_line,
                max_items=effective_max,
                raw_sink=raw_sink,
                full=full,
            )
        else:
            raise AerError(
                "UNSUPPORTED_FORMAT",
                "Inspection format is not supported.",
                operation="inspect",
                target=str(target),
                details={"extension": suffix or None},
            )
    result["target"] = str(target)
    if full:
        return result
    return enforce_output_budget(
        result,
        raw_sink=raw_sink,
        name=f"{path.name or 'repository'}.inspect-result.json",
    )
