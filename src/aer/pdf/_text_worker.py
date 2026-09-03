"""Private resource-limited PDF text worker. Invoked only through ``aer.pdf.safety``."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any

MAX_REQUEST_BYTES = 64 * 1024
MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_PAGES = 10_000
MAX_TEXT_BYTES = 1024 * 1024
MAX_QUERY_LINE_BYTES = 16 * 1024
MAX_QUERY_RESULT_BYTES = 1024 * 1024
MAX_SERIALIZED_OUTPUT_BYTES = 2 * MAX_TEXT_BYTES + 256 * 1024
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
CPU_LIMIT_SECONDS = 8


def _apply_posix_limits() -> None:
    if os.name != "posix":
        return
    try:
        import resource
    except ImportError:
        return

    def lower(kind: int, soft: int, hard: int) -> None:
        try:
            _current_soft, current_hard = resource.getrlimit(kind)
            effective_hard = (
                hard if current_hard == resource.RLIM_INFINITY else min(hard, current_hard)
            )
            effective_soft = min(soft, effective_hard)
            resource.setrlimit(kind, (effective_soft, effective_hard))
        except (OSError, ValueError):
            return

    if hasattr(resource, "RLIMIT_AS"):
        lower(resource.RLIMIT_AS, MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES)
    if hasattr(resource, "RLIMIT_CPU"):
        lower(resource.RLIMIT_CPU, CPU_LIMIT_SECONDS, CPU_LIMIT_SECONDS + 1)


def _utf8_prefix(value: str, limit: int) -> tuple[str, bool]:
    candidate = value[:limit]
    encoded = candidate.encode("utf-8", errors="replace")
    if len(encoded) <= limit and len(candidate) == len(value):
        return candidate, False
    prefix = encoded[:limit].decode("utf-8", errors="ignore")
    return prefix, True


def _line_count(value: str) -> int:
    return sum(1 for _line in io.StringIO(value))


def _utf8_size(value: str) -> int:
    return sum(
        len(value[offset : offset + 64 * 1024].encode("utf-8", errors="replace"))
        for offset in range(0, len(value), 64 * 1024)
    )


def _load_reader(path: Path) -> Any:
    from pypdf import PdfReader

    reader = PdfReader(path, strict=False)
    if reader.is_encrypted:
        raise PermissionError("Encrypted PDFs are not supported.")
    return reader


def _page_count(reader: Any) -> int:
    declared = int(reader.trailer["/Root"]["/Pages"]["/Count"])
    if declared < 0:
        raise ValueError("PDF page tree has a negative page count.")
    if declared > MAX_PAGES:
        raise OverflowError("PDF page tree exceeds the page safety limit.")
    actual = len(reader.pages)
    if actual > MAX_PAGES:
        raise OverflowError("PDF page tree exceeds the page safety limit.")
    return actual


def _extract_page(reader: Any, page: int) -> str:
    page_count = _page_count(reader)
    if page < 1 or page > page_count:
        raise IndexError(f"Page {page} is outside a {page_count}-page PDF.")
    return str(reader.pages[page - 1].extract_text() or "")


def _page_result(reader: Any, page: int) -> dict[str, Any]:
    text = _extract_page(reader, page)
    bounded, truncated = _utf8_prefix(text, MAX_TEXT_BYTES)
    return {
        "page": page,
        "text": bounded,
        "line_count": _line_count(text),
        "text_bytes": _utf8_size(text),
        "extraction_truncated": truncated,
    }


def _query_result(reader: Any, query: str) -> dict[str, Any]:
    _page_count(reader)
    folded = query.casefold()
    matches: list[dict[str, Any]] = []
    match_count = 0
    used = 0
    truncated = False
    for page_number, page in enumerate(reader.pages, start=1):
        text = str(page.extract_text() or "")
        for line_number, raw_line in enumerate(io.StringIO(text), start=1):
            line = raw_line.rstrip("\r\n")
            if folded not in line.casefold():
                continue
            match_count += 1
            bounded_line, line_truncated = _utf8_prefix(line, MAX_QUERY_LINE_BYTES)
            record = {
                "page": page_number,
                "line": line_number,
                "text": bounded_line,
            }
            if line_truncated:
                record["text_truncated"] = True
            encoded_size = len(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            if used + encoded_size <= MAX_QUERY_RESULT_BYTES:
                matches.append(record)
                used += encoded_size
            else:
                truncated = True
            truncated = truncated or line_truncated
    return {"query": query, "match_count": match_count, "matches": matches, "truncated": truncated}


def _presence_result(reader: Any, pages: Any) -> dict[str, Any]:
    if not isinstance(pages, list) or any(not isinstance(page, int) for page in pages):
        raise ValueError("Presence pages must be an integer array.")
    if len(pages) > 100:
        raise OverflowError("Presence page selection exceeds the safety limit.")
    return {
        "pages": [
            {"page": page, "has_text": bool(_extract_page(reader, page).strip())} for page in pages
        ]
    }


def _execute(request: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(request.get("path", "")))
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("PDF input must be a regular non-symlink file.")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise OverflowError("PDF input exceeds the first-version byte limit.")
    reader = _load_reader(path)
    mode = request.get("mode")
    if mode == "page":
        return _page_result(reader, int(request.get("page", 0)))
    if mode == "query":
        query = request.get("query")
        if not isinstance(query, str):
            raise ValueError("PDF query must be text.")
        return _query_result(reader, query)
    if mode == "presence":
        return _presence_result(reader, request.get("pages"))
    raise ValueError("Unknown PDF text worker mode.")


def _emit(payload: dict[str, Any], *, enforce_limit: bool = True) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if enforce_limit and len(encoded) > MAX_SERIALIZED_OUTPUT_BYTES:
        raise OverflowError("PDF text worker output exceeds the safety limit.")
    sys.stdout.buffer.write(encoded)


def main() -> int:
    _apply_posix_limits()
    try:
        request_bytes = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(request_bytes) > MAX_REQUEST_BYTES:
            raise OverflowError("PDF text worker request exceeds the safety limit.")
        request = json.loads(request_bytes.decode("utf-8"))
        if not isinstance(request, dict):
            raise ValueError("PDF text worker request must be an object.")
        result = _execute(request)
        _emit({"ok": True, "result": result})
        return 0
    except PermissionError as exc:
        _emit({"ok": False, "code": "UNSUPPORTED_FORMAT", "message": str(exc)})
    except IndexError as exc:
        _emit({"ok": False, "code": "INVALID_SELECTOR", "message": str(exc)})
    except (MemoryError, OverflowError) as exc:
        _emit(
            {"ok": False, "code": "LIMIT_EXCEEDED", "message": str(exc)},
            enforce_limit=False,
        )
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "code": "CORRUPT_FILE",
                "message": f"PDF text could not be extracted: {str(exc)[:300]}",
            }
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
