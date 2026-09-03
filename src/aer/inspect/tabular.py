"""Streaming-oriented CSV, TSV, and JSONL inspection."""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from pathlib import Path
from typing import Any

from aer.errors import AerError
from aer.inspect.common import RawSink, parse_inclusive_range, preserve_overflow, read_text
from aer.limits import MAX_TABULAR_CELLS


def inspect_tabular(
    path: Path,
    *,
    kind: str,
    selector: str | None,
    query: str | None,
    rows: str | None,
    max_items: int,
    raw_sink: RawSink | None,
) -> dict[str, Any]:
    text, encoding, bytes_read = read_text(path)
    warnings: list[str] = []
    if kind == "jsonl":
        records = _read_jsonl(text, path)
        headers = _jsonl_headers(records)
    else:
        delimiter = "\t" if kind == "tsv" else ","
        headers, records, duplicate = _read_delimited(text, delimiter=delimiter, path=path)
        if duplicate:
            warnings.append("Duplicate headers were disambiguated with #N suffixes.")
    cells = len(records) * len(headers)
    if cells > MAX_TABULAR_CELLS:
        raise AerError(
            "LIMIT_EXCEEDED",
            "Tabular inspection exceeds the row-by-column safety limit.",
            operation="inspect",
            target=str(path),
            details={
                "rows": len(records),
                "columns": len(headers),
                "cells": cells,
                "limit": MAX_TABULAR_CELLS,
            },
            suggested_action="Select or query a smaller local extract with aer data query.",
        )

    selected_headers = _selected_headers(selector, headers)
    row_start, row_end = (
        (1, len(records)) if rows is None else parse_inclusive_range(rows, target=rows)
    )
    chosen: list[dict[str, Any]] = []
    query_folded = query.casefold() if query is not None else None
    for number, record in enumerate(records, start=1):
        if number < row_start or number > row_end:
            continue
        if query_folded is not None and not any(
            query_folded in str(value).casefold() for value in record.values()
        ):
            continue
        chosen.append(
            {key: record.get(key) for key in selected_headers} if selected_headers else record
        )

    column_types: dict[str, Counter[str]] = {header: Counter() for header in headers}
    non_null_counts = Counter[str]()
    for record in records:
        for raw_header, value in record.items():
            header = str(raw_header)
            if value is not None and value != "":
                non_null_counts[header] += 1
                column_types[header][_infer_type(value)] += 1

    result: dict[str, Any] = {
        "type": kind,
        "encoding": encoding,
        "bytes": bytes_read,
        "row_count": len(records),
        "columns": [
            {
                "name": header,
                "types": sorted(column_types[header]),
                "null_count": len(records) - non_null_counts[header],
            }
            for header in headers
        ],
        "matched_rows": len(chosen),
        "preview": chosen[:max_items],
    }
    if warnings:
        result["warnings"] = warnings
    if selector is not None:
        result["selected_columns"] = selected_headers
    if rows is not None:
        result["row_range"] = {"start": row_start, "end": row_end}
    if query is not None:
        result["query"] = query
    if len(chosen) > max_items:
        result["truncated"] = True
        result["raw_ref"] = preserve_overflow(
            chosen, raw_sink=raw_sink, name=f"{path.name}.rows.json"
        )
    return result


def _read_delimited(
    text: str, *, delimiter: str, path: Path
) -> tuple[list[str], list[dict[str, str | None]], bool]:
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        first = next(reader, None)
        if first is None:
            return [], [], False
        headers, duplicate = _unique_headers(first)
        records: list[dict[str, str | None]] = []
        for values in reader:
            row: dict[str, str | None] = {}
            for index, header in enumerate(headers):
                row[header] = values[index] if index < len(values) else None
            records.append(row)
        return headers, records, duplicate
    except csv.Error as exc:
        raise AerError(
            "CORRUPT_FILE",
            "Delimited data could not be parsed.",
            operation="inspect",
            target=str(path),
            details={"error": str(exc)},
        ) from exc


def _read_jsonl(text: str, path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AerError(
                "CORRUPT_FILE",
                "JSONL contains an invalid JSON record.",
                operation="inspect",
                target=f"{path}:{line_number}",
                details={"error": str(exc).splitlines()[0]},
            ) from exc
        if isinstance(value, dict):
            records.append(value)
        else:
            records.append({"value": value})
    return records


def _jsonl_headers(records: list[dict[str, Any]]) -> list[str]:
    keys: set[str] = set()
    for record in records:
        keys.update(str(key) for key in record)
    return sorted(keys)


def _unique_headers(headers: list[str]) -> tuple[list[str], bool]:
    counts: Counter[str] = Counter()
    result: list[str] = []
    duplicate = False
    for raw in headers:
        header = raw if raw else "column"
        counts[header] += 1
        if counts[header] > 1:
            duplicate = True
            header = f"{header}#{counts[header]}"
        result.append(header)
    return result, duplicate


def _selected_headers(selector: str | None, headers: list[str]) -> list[str]:
    if selector is None:
        return []
    requested = [value.strip() for value in selector.split(",") if value.strip()]
    if not requested:
        raise AerError(
            "INVALID_SELECTOR",
            "Column selector must contain at least one column name.",
            operation="inspect",
            target=selector,
        )
    missing = [value for value in requested if value not in headers]
    if missing:
        raise AerError(
            "INVALID_SELECTOR",
            "Selected column was not found.",
            operation="inspect",
            target=selector,
            details={"missing": missing, "available": headers[:50]},
        )
    return requested


def _infer_type(value: Any) -> str:
    if not isinstance(value, str):
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "number"
        if isinstance(value, (dict, list)):
            return "structured"
        return type(value).__name__
    folded = value.casefold()
    if folded in {"true", "false"}:
        return "boolean"
    try:
        int(value)
        return "integer"
    except ValueError:
        pass
    try:
        float(value)
        return "number"
    except ValueError:
        return "string"
