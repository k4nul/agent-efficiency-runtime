from __future__ import annotations

import re
from contextlib import suppress
from pathlib import Path
from typing import IO, Any, BinaryIO, cast

from pypdf import PdfReader, PdfWriter

from aer.errors import AerError
from aer.hashing import sha256_file
from aer.limits import (
    MAX_PDF_INPUT_FILES,
    MAX_PDF_OUTPUT_BYTES,
    MAX_PDF_PAGES,
    MAX_PDF_SPLIT_FILES,
)
from aer.paths import atomic_binary_writer, prepare_output_path
from aer.pdf.safety import (
    bounded_pdf_page_count,
    enforce_pdf_aggregate_input_limit,
    ensure_bounded_pdf_input,
    extract_pdf_page_text,
)


class _BoundedPdfOutput:
    """File-like adapter that stops pypdf before an output budget is exceeded."""

    def __init__(
        self,
        handle: BinaryIO,
        *,
        operation: str,
        target: Path,
        bytes_before: int,
    ) -> None:
        self._handle = handle
        self._operation = operation
        self._target = target
        self._bytes_before = bytes_before
        self._high_water = 0

    @property
    def bytes_written(self) -> int:
        return self._high_water

    def tell(self) -> int:
        return self._handle.tell()

    def write(self, data: bytes) -> int:
        projected = max(self._high_water, self.tell() + len(data))
        aggregate = self._bytes_before + projected
        if aggregate > MAX_PDF_OUTPUT_BYTES:
            raise AerError(
                "LIMIT_EXCEEDED",
                "PDF output exceeds the first-version byte limit.",
                self._operation,
                str(self._target),
                {
                    "bytes_attempted": aggregate,
                    "limit": MAX_PDF_OUTPUT_BYTES,
                },
                "Select fewer pages or process the PDF in smaller batches.",
            )
        written = self._handle.write(data)
        self._high_water = max(self._high_water, self.tell())
        return written

    def flush(self) -> None:
        self._handle.flush()


def _reader(path: Path, operation: str) -> tuple[Path, PdfReader]:
    source = ensure_bounded_pdf_input(path, operation=operation)
    try:
        reader = PdfReader(source)
    except Exception as exc:
        raise AerError("CORRUPT_FILE", f"Cannot open PDF: {exc}", operation, str(source)) from exc
    if reader.is_encrypted:
        raise AerError(
            "UNSUPPORTED_FORMAT", "Encrypted PDFs are not supported.", operation, str(source)
        )
    return source, reader


def _different_output(inputs: list[Path], output: Path, operation: str) -> Path:
    resolved = prepare_output_path(output, operation=operation)
    if resolved.suffix.lower() != ".pdf":
        raise AerError(
            "INVALID_ARGUMENT",
            "PDF output must use the .pdf extension.",
            operation,
            str(resolved),
            suggested_action="Choose an output path ending in .pdf.",
        )
    if any(source.resolve() == resolved for source in inputs):
        raise AerError(
            "CONFLICT", "Output must differ from every input file.", operation, str(resolved)
        )
    return resolved


def _metadata(reader: PdfReader) -> dict[str, str]:
    return {
        str(key): str(value) for key, value in (reader.metadata or {}).items() if value is not None
    }


def parse_pages(selector: str, page_count: int) -> list[int]:
    if not selector or len(selector) > 1000:
        raise AerError(
            "INVALID_SELECTOR", "Page selector is empty or too long.", "pdf.pages", selector
        )
    pages: list[int] = []
    seen: set[int] = set()
    for token in selector.split(","):
        token = token.strip()
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", token)
        if not match:
            raise AerError(
                "INVALID_SELECTOR", "Page selector must look like 1-5,8.", "pdf.pages", selector
            )
        start, end = int(match.group(1)), int(match.group(2) or match.group(1))
        if start < 1 or end < start or end > page_count:
            raise AerError(
                "INVALID_SELECTOR", "Page selector is outside the document.", "pdf.pages", token
            )
        for page in range(start, end + 1):
            if page not in seen:
                if len(pages) >= MAX_PDF_PAGES:
                    raise AerError(
                        "LIMIT_EXCEEDED",
                        "Page selection exceeds the first-version page limit.",
                        "pdf.pages",
                        selector,
                        {"pages": len(pages) + 1, "limit": MAX_PDF_PAGES},
                        f"Select at most {MAX_PDF_PAGES:,} pages in one operation.",
                    )
                seen.add(page)
                pages.append(page - 1)
    return pages


def _write(
    writer: PdfWriter,
    output: Path,
    *,
    operation: str,
    bytes_before: int = 0,
) -> int:
    """Atomically stream a PDF while enforcing the aggregate operation budget."""

    with atomic_binary_writer(output) as handle:
        bounded = _BoundedPdfOutput(
            handle,
            operation=operation,
            target=output,
            bytes_before=bytes_before,
        )
        writer.write(cast(IO[Any], bounded))
        return bounded.bytes_written


def inspect_pdf(path: Path, *, page: int | None = None) -> dict[str, Any]:
    source, reader = _reader(path, "pdf.inspect")
    page_count = bounded_pdf_page_count(
        reader,
        operation="pdf.inspect",
        path=source,
        limit=MAX_PDF_PAGES,
    )
    result: dict[str, Any] = {
        "path": str(source),
        "page_count": page_count,
        "encrypted": reader.is_encrypted,
        "metadata": _metadata(reader),
        "attachments": sorted((reader.attachments or {}).keys())[:20],
    }
    if page is not None:
        if page < 1 or page > page_count:
            raise AerError(
                "INVALID_SELECTOR",
                "PDF page is outside the document.",
                "pdf.inspect",
                f"page:{page}",
            )
        selected = reader.pages[page - 1]
        extracted = extract_pdf_page_text(source, page, operation="pdf.inspect")
        text = str(extracted["text"])
        result["page"] = {
            "number": page,
            "width": float(selected.mediabox.width),
            "height": float(selected.mediabox.height),
            "text_preview": text[:4000],
            "text_truncated": bool(extracted.get("extraction_truncated")) or len(text) > 4000,
            "line_count": int(extracted["line_count"]),
            "extracted_text_bytes": int(extracted["text_bytes"]),
        }
    else:
        result["pages"] = [
            {
                "number": index,
                "width": float(item.mediabox.width),
                "height": float(item.mediabox.height),
            }
            for index, item in enumerate(reader.pages[:20], start=1)
        ]
    return result


def merge_pdfs(inputs: list[Path], output: Path) -> dict[str, Any]:
    if len(inputs) < 2:
        raise AerError("INVALID_ARGUMENT", "PDF merge requires at least two inputs.", "pdf.merge")
    if len(inputs) > MAX_PDF_INPUT_FILES:
        raise AerError(
            "LIMIT_EXCEEDED",
            "PDF merge has too many input files.",
            "pdf.merge",
            details={"inputs": len(inputs), "limit": MAX_PDF_INPUT_FILES},
            suggested_action=f"Merge at most {MAX_PDF_INPUT_FILES:,} PDFs at a time.",
        )
    bounded_sources = [ensure_bounded_pdf_input(path, operation="pdf.merge") for path in inputs]
    enforce_pdf_aggregate_input_limit(bounded_sources, operation="pdf.merge")
    opened = [_reader(path, "pdf.merge") for path in bounded_sources]
    sources = [item[0] for item in opened]
    destination = _different_output(sources, output, "pdf.merge")
    page_counts: list[int] = []
    total_pages = 0
    for source, reader in opened:
        count = bounded_pdf_page_count(
            reader,
            operation="pdf.merge",
            path=source,
            limit=MAX_PDF_PAGES,
        )
        total_pages += count
        if total_pages > MAX_PDF_PAGES:
            raise AerError(
                "LIMIT_EXCEEDED",
                "Merged PDF would exceed the first-version page limit.",
                "pdf.merge",
                str(destination),
                {"pages": total_pages, "limit": MAX_PDF_PAGES},
                "Merge fewer pages or create multiple bounded outputs.",
            )
        page_counts.append(count)
    writer = PdfWriter()
    for (_, reader), count in zip(opened, page_counts, strict=True):
        for index in range(count):
            writer.add_page(reader.pages[index])
    metadata = _metadata(opened[0][1])
    if metadata:
        writer.add_metadata(metadata)
    bytes_written = _write(writer, destination, operation="pdf.merge")
    return {
        "output": str(destination),
        "page_count": len(writer.pages),
        "bytes_written": bytes_written,
        "sha256": sha256_file(destination),
    }


def extract_pdf(path: Path, output: Path, *, pages: str) -> dict[str, Any]:
    source, reader = _reader(path, "pdf.extract")
    destination = _different_output([source], output, "pdf.extract")
    page_count = bounded_pdf_page_count(
        reader,
        operation="pdf.extract",
        path=source,
        limit=MAX_PDF_PAGES,
    )
    selected = parse_pages(pages, page_count)
    writer = PdfWriter()
    for index in selected:
        writer.add_page(reader.pages[index])
    metadata = _metadata(reader)
    if metadata:
        writer.add_metadata(metadata)
    bytes_written = _write(writer, destination, operation="pdf.extract")
    return {
        "output": str(destination),
        "pages": [index + 1 for index in selected],
        "page_count": len(selected),
        "bytes_written": bytes_written,
        "sha256": sha256_file(destination),
    }


def split_pdf(path: Path, output_dir: Path) -> dict[str, Any]:
    source, reader = _reader(path, "pdf.split")
    page_count = bounded_pdf_page_count(
        reader,
        operation="pdf.split",
        path=source,
        limit=MAX_PDF_SPLIT_FILES,
    )
    destination = prepare_output_path(output_dir, operation="pdf.split")
    expected_outputs = [
        destination / f"{source.stem}-{index:04d}.pdf" for index in range(1, page_count + 1)
    ]
    for output in expected_outputs:
        if output.exists():
            raise AerError("CONFLICT", "Split output already exists.", "pdf.split", str(output))
    destination_existed = destination.exists()
    destination.mkdir(parents=True, exist_ok=True)
    outputs = []
    created: list[Path] = []
    total_bytes = 0
    metadata = _metadata(reader)
    try:
        for index, output in enumerate(expected_outputs, start=1):
            writer = PdfWriter()
            writer.add_page(reader.pages[index - 1])
            if metadata:
                writer.add_metadata(metadata)
            size = _write(
                writer,
                output,
                operation="pdf.split",
                bytes_before=total_bytes,
            )
            created.append(output)
            total_bytes += size
            outputs.append(
                {
                    "page": index,
                    "output": str(output),
                    "sha256": sha256_file(output),
                }
            )
    except BaseException:
        for created_output in created:
            created_output.unlink(missing_ok=True)
        if not destination_existed:
            with suppress(OSError):
                destination.rmdir()
        raise
    return {
        "output_dir": str(destination),
        "count": len(outputs),
        "bytes_written": total_bytes,
        "files": outputs[:20],
    }
