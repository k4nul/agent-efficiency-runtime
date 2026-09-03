"""Bounded plain-text inspection."""

from __future__ import annotations

import json
import re
import tempfile
from collections import deque
from collections.abc import Iterator
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
_MAX_TEXT_LINE_CHARS = 1024 * 1024
_LINE_BREAK_RE = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


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
    result: dict[str, Any] = {
        "type": "text",
        "encoding": encoding,
        "bytes": bytes_read,
    }

    if start_line is not None or end_line is not None:
        start = 1 if start_line is None else start_line
        if start < 1 or (end_line is not None and end_line < start):
            raise AerError(
                "INVALID_SELECTOR",
                "Line range must be one-based and end at or after start.",
                operation="inspect",
                target=f"{start}:{end_line or ''}",
            )
        preview: list[dict[str, Any]] = []
        selected_count = 0
        selected_end = start - 1
        text_truncated = False
        line_count = 0
        with _MatchSpool(
            raw_sink=raw_sink,
            name=f"{path.name}.lines.json",
            target=path,
        ) as spool:
            for line_count, line in enumerate(_iter_text_lines(text, path=path), start=1):
                if line_count < start or (end_line is not None and line_count > end_line):
                    continue
                exact = {"line": line_count, "text": line}
                spool.append(exact)
                selected_count += 1
                selected_end = line_count
                if len(preview) < max_items:
                    item = exact if full else _compact_line_record(exact)
                    preview.append(item)
                    text_truncated = text_truncated or bool(item.get("text_truncated"))
            result["line_count"] = line_count
            result["range"] = {"start": start, "end": selected_end}
            result["preview"] = preview
            if selected_count > max_items or text_truncated:
                result["truncated"] = True
                result["raw_ref"] = spool.preserve()
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
            before: deque[dict[str, Any]] = deque(maxlen=context)
            pending: deque[dict[str, Any]] = deque()
            line_count = 0

            def consume(exact: dict[str, Any]) -> None:
                nonlocal match_count, text_truncated
                spool.append(exact)
                match_count += 1
                if len(matches) < max_items:
                    preview_record = exact if full else _compact_match_record(exact)
                    matches.append(preview_record)
                    text_truncated = text_truncated or _record_has_truncated_text(preview_record)

            for line_count, line in enumerate(_iter_text_lines(text, path=path), start=1):
                for exact in pending:
                    after = exact["after"]
                    if isinstance(after, list):
                        after.append({"line": line_count, "text": line})
                while pending and line_count - int(pending[0]["line"]) >= context:
                    consume(pending.popleft())
                if not line_matches(
                    line,
                    query,
                    regex=compiled,
                    case_sensitive=case_sensitive,
                ):
                    before.append({"line": line_count, "text": line})
                    continue
                exact = {
                    "line": line_count,
                    "text": line,
                    "before": list(before),
                    "after": [],
                }
                if context:
                    pending.append(exact)
                else:
                    consume(exact)
                before.append({"line": line_count, "text": line})
            while pending:
                consume(pending.popleft())
            result["line_count"] = line_count
            result["query"] = query
            result["match_count"] = match_count
            result["matches"] = matches
            if match_count > max_items or text_truncated:
                result["truncated"] = True
                result["raw_ref"] = spool.preserve()
        return result

    exact_preview: list[dict[str, Any]] = []
    line_count = 0
    for line_count, line in enumerate(_iter_text_lines(text, path=path), start=1):
        if len(exact_preview) < max_items:
            exact_preview.append({"line": line_count, "text": line})
    result["line_count"] = line_count
    preview = exact_preview if full else [_compact_line_record(item) for item in exact_preview]
    result["preview"] = preview
    text_truncated = any(item.get("text_truncated", False) for item in preview)
    if line_count > max_items or text_truncated:
        result["truncated"] = True
    if text_truncated:
        result["raw_ref"] = preserve_overflow(
            exact_preview, raw_sink=raw_sink, name=f"{path.name}.preview.json"
        )
    return result


def _iter_text_lines(text: str, *, path: Path) -> Iterator[str]:
    """Yield logical lines without constructing a second file-sized list."""

    start = 0
    line_number = 0
    length = len(text)
    while start < length:
        separator = _LINE_BREAK_RE.search(text, start)
        if separator is None:
            end = length
            next_start = length
        else:
            end = separator.start()
            next_start = separator.end()
        line_number += 1
        line_length = end - start
        if line_length > _MAX_TEXT_LINE_CHARS:
            raise AerError(
                "LIMIT_EXCEEDED",
                "Text line exceeds the inspection safety limit.",
                operation="inspect",
                target=str(path),
                details={
                    "line": line_number,
                    "characters": line_length,
                    "limit": _MAX_TEXT_LINE_CHARS,
                },
                suggested_action="Inspect a bounded source segment or reformat the input.",
            )
        yield text[start:end]
        start = next_start


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
