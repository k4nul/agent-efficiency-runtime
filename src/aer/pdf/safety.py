"""Bounded PDF input and isolated text-extraction helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from aer.errors import AerError
from aer.limits import MAX_PDF_PAGES
from aer.paths import ensure_regular_input

MAX_PDF_INPUT_BYTES = 256 * 1024 * 1024
MAX_PDF_AGGREGATE_INPUT_BYTES = 512 * 1024 * 1024
MAX_PDF_EXTRACTED_TEXT_BYTES = 1024 * 1024
MAX_PDF_TEXT_VALIDATION_PAGES = 100
PDF_TEXT_TIMEOUT_SECONDS = 10
_MAX_WORKER_REQUEST_BYTES = 64 * 1024
_MAX_WORKER_OUTPUT_BYTES = 2 * MAX_PDF_EXTRACTED_TEXT_BYTES + 256 * 1024
_run_subprocess = subprocess.run


def ensure_bounded_pdf_input(path: Path, *, operation: str) -> Path:
    """Resolve one regular PDF input and reject it before parsing when it is too large."""

    source = ensure_regular_input(path, operation=operation)
    size = source.stat().st_size
    if size > MAX_PDF_INPUT_BYTES:
        raise AerError(
            "LIMIT_EXCEEDED",
            "PDF input exceeds the first-version byte limit.",
            operation,
            str(source),
            {"bytes": size, "limit": MAX_PDF_INPUT_BYTES},
            "Process a smaller PDF or split it before using AER.",
        )
    return source


def enforce_pdf_aggregate_input_limit(paths: Sequence[Path], *, operation: str) -> None:
    """Bound the aggregate bytes opened by a multi-PDF operation."""

    total = sum(path.stat().st_size for path in paths)
    if total > MAX_PDF_AGGREGATE_INPUT_BYTES:
        raise AerError(
            "LIMIT_EXCEEDED",
            "Aggregate PDF input exceeds the first-version byte limit.",
            operation,
            details={"bytes": total, "limit": MAX_PDF_AGGREGATE_INPUT_BYTES},
            suggested_action="Merge fewer PDFs or process them in bounded batches.",
        )


def bounded_pdf_page_count(
    reader: Any,
    *,
    path: Path,
    operation: str,
    limit: int = MAX_PDF_PAGES,
) -> int:
    """Reject an excessive declared PDF page tree before expanding ``reader.pages``."""

    try:
        declared = int(reader.trailer["/Root"]["/Pages"]["/Count"])
    except Exception as exc:
        raise AerError(
            "CORRUPT_FILE",
            "PDF page tree has no valid page count.",
            operation,
            str(path),
        ) from exc
    if declared < 0:
        raise AerError(
            "CORRUPT_FILE",
            "PDF page tree has an invalid negative page count.",
            operation,
            str(path),
        )
    if declared > limit:
        raise AerError(
            "LIMIT_EXCEEDED",
            "PDF page count exceeds the first-version operation limit.",
            operation,
            str(path),
            {"pages": declared, "limit": limit},
            "Process the PDF in bounded page batches.",
        )
    try:
        actual = len(reader.pages)
    except Exception as exc:
        raise AerError(
            "CORRUPT_FILE",
            "Cannot read the PDF page tree.",
            operation,
            str(path),
        ) from exc
    if actual > limit:
        raise AerError(
            "LIMIT_EXCEEDED",
            "PDF page count exceeds the first-version operation limit.",
            operation,
            str(path),
            {"pages": actual, "limit": limit},
            "Process the PDF in bounded page batches.",
        )
    return actual


def extract_pdf_page_text(path: Path, page: int, *, operation: str) -> dict[str, Any]:
    """Extract one page in a resource-limited subprocess and return bounded text."""

    return _run_text_worker(path, {"mode": "page", "page": page}, operation=operation)


def search_pdf_text(path: Path, query: str, *, operation: str) -> dict[str, Any]:
    """Search PDF text in a resource-limited subprocess with bounded match output."""

    return _run_text_worker(path, {"mode": "query", "query": query}, operation=operation)


def pdf_text_presence(path: Path, pages: Sequence[int], *, operation: str) -> dict[int, bool]:
    """Return whether selected pages contain text without exposing their content."""

    if len(pages) > MAX_PDF_TEXT_VALIDATION_PAGES:
        raise AerError(
            "LIMIT_EXCEEDED",
            "PDF text validation page selection exceeds the safety limit.",
            operation,
            str(path),
            {"pages": len(pages), "limit": MAX_PDF_TEXT_VALIDATION_PAGES},
        )
    if not pages:
        return {}
    result = _run_text_worker(
        path,
        {"mode": "presence", "pages": list(pages)},
        operation=operation,
    )
    records = result.get("pages")
    if not isinstance(records, list):
        raise _worker_protocol_error(path, operation)
    presence: dict[int, bool] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("page"), int):
            raise _worker_protocol_error(path, operation)
        presence[int(record["page"])] = bool(record.get("has_text"))
    if set(presence) != set(pages):
        raise _worker_protocol_error(path, operation)
    return presence


def _run_text_worker(path: Path, request: dict[str, Any], *, operation: str) -> dict[str, Any]:
    source = ensure_bounded_pdf_input(path, operation=operation)
    request = {**request, "path": str(source)}
    encoded_request = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded_request) > _MAX_WORKER_REQUEST_BYTES:
        raise AerError(
            "LIMIT_EXCEEDED",
            "PDF text extraction request exceeds the safety limit.",
            operation,
            str(source),
            {"bytes": len(encoded_request), "limit": _MAX_WORKER_REQUEST_BYTES},
        )
    try:
        completed = _run_subprocess(
            [sys.executable, str(Path(__file__).with_name("_text_worker.py"))],
            input=encoded_request,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=PDF_TEXT_TIMEOUT_SECONDS,
            check=False,
            shell=False,
            start_new_session=os.name == "posix",
        )
    except subprocess.TimeoutExpired as exc:
        raise AerError(
            "LIMIT_EXCEEDED",
            "PDF text extraction exceeded the safety timeout.",
            operation,
            str(source),
            {"timeout_seconds": PDF_TEXT_TIMEOUT_SECONDS},
            "Inspect fewer pages or use a PDF with simpler page content.",
        ) from exc
    stdout = completed.stdout or b""
    if len(stdout) > _MAX_WORKER_OUTPUT_BYTES:
        raise AerError(
            "LIMIT_EXCEEDED",
            "PDF text extraction exceeded the output safety limit.",
            operation,
            str(source),
            {"bytes": len(stdout), "limit": _MAX_WORKER_OUTPUT_BYTES},
        )
    try:
        payload = json.loads(stdout.decode("utf-8")) if stdout else None
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _worker_protocol_error(source, operation) from exc
    if not isinstance(payload, dict):
        if completed.returncode < 0:
            raise AerError(
                "LIMIT_EXCEEDED",
                "PDF text extraction was stopped by a resource limit.",
                operation,
                str(source),
                {"returncode": completed.returncode},
            )
        raise _worker_protocol_error(source, operation)
    if not payload.get("ok"):
        code = str(payload.get("code", "CORRUPT_FILE"))
        if code not in {
            "CORRUPT_FILE",
            "INVALID_SELECTOR",
            "LIMIT_EXCEEDED",
            "UNSUPPORTED_FORMAT",
        }:
            code = "CORRUPT_FILE"
        details = payload.get("details")
        raise AerError(
            code,
            str(payload.get("message", "PDF text extraction failed."))[:500],
            operation,
            str(source),
            cast(dict[str, Any], details) if isinstance(details, dict) else {},
        )
    if completed.returncode != 0 or not isinstance(payload.get("result"), dict):
        raise _worker_protocol_error(source, operation)
    return cast(dict[str, Any], payload["result"])


def _worker_protocol_error(path: Path, operation: str) -> AerError:
    return AerError(
        "CORRUPT_FILE",
        "PDF text extraction worker returned an invalid response.",
        operation,
        str(path),
    )
