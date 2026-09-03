"""Bounded plain-text inspection."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO

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

_MAX_TEXT_MATCHES = 100_000
_MAX_TEXT_MATCH_BYTES = 64 * 1024 * 1024


class _MatchSpool:
    """Write exact match records to a private bounded temporary JSON array."""

    def __init__(self, *, raw_sink: RawSink | None, name: str, target: Path) -> None:
        self._raw_sink = raw_sink
        self._name = name
        self._target = target
        # This object owns the stream and closes it in __exit__.
        self._stream: BinaryIO = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115
        self._stream.write(b"[")
        self._bytes = 1
        self._count = 0
        self._finalized = False

    def __enter__(self) -> _MatchSpool:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self._stream.close()

    def append(self, record: dict[str, Any]) -> None:
        encoded = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        separator = b"," if self._count else b""
        observed = self._count + 1
        if observed > _MAX_TEXT_MATCHES or self._bytes + len(separator) + len(encoded) + 1 > (
            _MAX_TEXT_MATCH_BYTES
        ):
            raw_ref = self.preserve()
            raise AerError(
                "LIMIT_EXCEEDED",
                "Text query matches exceed the exact-result safety limit.",
                operation="inspect",
                target=str(self._target),
                details={
                    "match_limit": _MAX_TEXT_MATCHES,
                    "encoded_bytes_limit": _MAX_TEXT_MATCH_BYTES,
                    "observed_at_least": observed,
                    "partial_results": self._count,
                },
                suggested_action="Narrow the query or select a smaller line range.",
                raw_ref=raw_ref,
            )
        self._stream.write(separator)
        self._stream.write(encoded)
        self._bytes += len(separator) + len(encoded)
        self._count = observed

    def preserve(self) -> str | None:
        if not self._finalized:
            self._stream.write(b"]")
            self._bytes += 1
            self._finalized = True
        if self._raw_sink is None:
            return None
        self._stream.flush()
        self._stream.seek(0)
        return self._raw_sink(self._stream.read(), self._name)


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
        matches: list[dict[str, Any]] = []
        match_count = 0
        text_truncated = False
        with _MatchSpool(
            raw_sink=raw_sink,
            name=f"{path.name}.matches.json",
            target=path,
        ) as spool:
            for index, line in enumerate(lines):
                if not line_matches(
                    line,
                    query,
                    regex=compiled,
                    case_sensitive=case_sensitive,
                ):
                    continue
                before_start = max(0, index - context)
                after_end = min(len(lines), index + context + 1)
                exact = {
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
                spool.append(exact)
                match_count += 1
                if len(matches) < max_items:
                    preview_record = exact if full else _compact_match_record(exact)
                    matches.append(preview_record)
                    text_truncated = text_truncated or _record_has_truncated_text(preview_record)
            result["query"] = query
            result["match_count"] = match_count
            result["matches"] = matches
            if match_count > max_items or text_truncated:
                result["truncated"] = True
                result["raw_ref"] = spool.preserve()
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
