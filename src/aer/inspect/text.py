"""Bounded plain-text inspection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aer.errors import AerError
from aer.inspect.common import (
    RawSink,
    compact_line,
    is_sensitive_path,
    line_matches,
    preserve_overflow,
    read_text,
    safe_regex,
)


def inspect_text(
    path: Path,
    *,
    query: str | None,
    regex: bool,
    case_sensitive: bool,
    context: int,
    start_line: int | None,
    end_line: int | None,
    max_items: int,
    raw_sink: RawSink | None,
    full: bool = False,
) -> dict[str, Any]:
    size = path.stat().st_size
    if is_sensitive_path(path):
        return {
            "type": "text",
            "bytes": size,
            "sensitive": True,
            "content_omitted": True,
        }
    text, encoding, bytes_read = read_text(path)
    lines = text.splitlines()
    result: dict[str, Any] = {
        "type": "text",
        "encoding": encoding,
        "bytes": bytes_read,
        "line_count": len(lines),
    }

    if start_line is not None or end_line is not None:
        start = 1 if start_line is None else start_line
        end = len(lines) if end_line is None else end_line
        if start < 1 or end < start:
            raise AerError(
                "INVALID_SELECTOR",
                "Line range must be one-based and end at or after start.",
                operation="inspect",
                target=f"{start}:{end}",
            )
        exact_selected = [
            {"line": number, "text": lines[number - 1]}
            for number in range(start, min(end, len(lines)) + 1)
        ]
        selected = (
            exact_selected if full else [_compact_line_record(item) for item in exact_selected]
        )
        text_truncated = any(item.get("text_truncated", False) for item in selected)
        result["range"] = {"start": start, "end": min(end, len(lines))}
        result["preview"] = selected[:max_items]
        if len(selected) > max_items or text_truncated:
            result["truncated"] = True
            result["raw_ref"] = preserve_overflow(
                exact_selected, raw_sink=raw_sink, name=f"{path.name}.lines.json"
            )
        return result

    if query is not None:
        if not query:
            raise AerError(
                "INVALID_ARGUMENT",
                "Text query must not be empty.",
                operation="inspect",
                target=str(path),
            )
        compiled = safe_regex(query, case_sensitive=case_sensitive) if regex else None
        exact_matches: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            if line_matches(
                line,
                query,
                regex=compiled,
                case_sensitive=case_sensitive,
            ):
                before_start = max(0, index - context)
                after_end = min(len(lines), index + context + 1)
                exact_matches.append(
                    {
                        "line": index + 1,
                        "text": line,
                        "before": [
                            {"line": number + 1, "text": lines[number]}
                            for number in range(before_start, index)
                        ],
                        "after": [
                            {"line": number + 1, "text": lines[number]}
                            for number in range(index + 1, after_end)
                        ],
                    }
                )
        matches = exact_matches if full else [_compact_match_record(item) for item in exact_matches]
        text_truncated = any(_record_has_truncated_text(item) for item in matches)
        result["query"] = query
        result["match_count"] = len(matches)
        result["matches"] = matches[:max_items]
        if len(matches) > max_items or text_truncated:
            result["truncated"] = True
            result["raw_ref"] = preserve_overflow(
                exact_matches, raw_sink=raw_sink, name=f"{path.name}.matches.json"
            )
        return result

    exact_preview = [
        {"line": index + 1, "text": line} for index, line in enumerate(lines[:max_items])
    ]
    preview = exact_preview if full else [_compact_line_record(item) for item in exact_preview]
    result["preview"] = preview
    text_truncated = any(item.get("text_truncated", False) for item in preview)
    if len(lines) > max_items or text_truncated:
        result["truncated"] = True
    if text_truncated:
        result["raw_ref"] = preserve_overflow(
            exact_preview, raw_sink=raw_sink, name=f"{path.name}.preview.json"
        )
    return result


def _compact_line_record(record: dict[str, Any]) -> dict[str, Any]:
    exact = str(record["text"])
    preview = compact_line(exact)
    result = {**record, "text": preview}
    if preview != exact.rstrip("\r\n"):
        result["text_truncated"] = True
    return result


def _compact_match_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        **_compact_line_record({"line": record["line"], "text": record["text"]}),
        "before": [_compact_line_record(item) for item in record["before"]],
        "after": [_compact_line_record(item) for item in record["after"]],
    }


def _record_has_truncated_text(record: dict[str, Any]) -> bool:
    return bool(
        record.get("text_truncated")
        or any(item.get("text_truncated") for item in record.get("before", []))
        or any(item.get("text_truncated") for item in record.get("after", []))
    )
