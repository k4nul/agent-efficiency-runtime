from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter

import aer.pdf.ops as pdf_ops
import aer.pdf.safety as pdf_safety
from aer.errors import AerError


def _pdf(path: Path, pages: int, *, title: str = "Bounded PDF") -> Path:
    writer = PdfWriter()
    writer.add_metadata({"/Title": title})
    for _ in range(pages):
        writer.add_blank_page(width=600, height=800)
    writer.write(path)
    return path


def test_page_selector_stops_before_expanding_an_excessive_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdf_ops, "MAX_PDF_PAGES", 2)

    with pytest.raises(AerError) as caught:
        pdf_ops.parse_pages("1-3", 3)

    assert caught.value.code == "LIMIT_EXCEEDED"
    assert caught.value.details == {"pages": 3, "limit": 2}
    assert caught.value.suggested_action == "Select at most 2 pages in one operation."


def test_declared_pdf_page_count_is_rejected_before_page_tree_expansion(tmp_path: Path) -> None:
    class Reader:
        def __init__(self) -> None:
            self.trailer = {"/Root": {"/Pages": {"/Count": 2}}}

        @property
        def pages(self) -> object:
            raise AssertionError("page tree must not be materialized")

    with pytest.raises(AerError) as caught:
        pdf_safety.bounded_pdf_page_count(
            Reader(), path=tmp_path / "declared.pdf", operation="pdf.inspect", limit=1
        )

    assert caught.value.code == "LIMIT_EXCEEDED"
    assert caught.value.details == {"pages": 2, "limit": 1}


def test_merge_rejects_excessive_inputs_and_total_pages_before_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = _pdf(tmp_path / "first.pdf", 2)
    second = _pdf(tmp_path / "second.pdf", 1)
    destination = tmp_path / "merged.pdf"

    monkeypatch.setattr(pdf_ops, "MAX_PDF_INPUT_FILES", 1)
    with pytest.raises(AerError) as too_many_inputs:
        pdf_ops.merge_pdfs([first, second], destination)
    assert too_many_inputs.value.code == "LIMIT_EXCEEDED"
    assert too_many_inputs.value.details == {"inputs": 2, "limit": 1}
    assert not destination.exists()

    monkeypatch.setattr(pdf_ops, "MAX_PDF_INPUT_FILES", 100)
    monkeypatch.setattr(pdf_ops, "MAX_PDF_PAGES", 2)
    with pytest.raises(AerError) as too_many_pages:
        pdf_ops.merge_pdfs([first, second], destination)
    assert too_many_pages.value.code == "LIMIT_EXCEEDED"
    assert too_many_pages.value.details == {"pages": 3, "limit": 2}
    assert not destination.exists()


def test_merge_and_extract_require_a_pdf_output_extension(tmp_path: Path) -> None:
    first = _pdf(tmp_path / "first.pdf", 1)
    second = _pdf(tmp_path / "second.pdf", 1)

    with pytest.raises(AerError) as merge_error:
        pdf_ops.merge_pdfs([first, second], tmp_path / "merged.txt")
    assert merge_error.value.code == "INVALID_ARGUMENT"
    assert not (tmp_path / "merged.txt").exists()

    with pytest.raises(AerError) as extract_error:
        pdf_ops.extract_pdf(first, tmp_path / "extracted.bin", pages="1")
    assert extract_error.value.code == "INVALID_ARGUMENT"
    assert not (tmp_path / "extracted.bin").exists()


def test_merge_streaming_byte_limit_preserves_existing_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = _pdf(tmp_path / "first.pdf", 1)
    second = _pdf(tmp_path / "second.pdf", 1)
    destination = tmp_path / "merged.pdf"
    destination.write_bytes(b"existing output")
    monkeypatch.setattr(pdf_ops, "MAX_PDF_OUTPUT_BYTES", 64)

    with pytest.raises(AerError) as caught:
        pdf_ops.merge_pdfs([first, second], destination)

    assert caught.value.code == "LIMIT_EXCEEDED"
    assert caught.value.details["limit"] == 64
    assert caught.value.details["bytes_attempted"] > 64
    assert destination.read_bytes() == b"existing output"
    assert not list(tmp_path.glob(".merged.pdf.*.tmp"))


def test_split_rejects_fan_out_before_creating_the_output_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _pdf(tmp_path / "source.pdf", 2)
    destination = tmp_path / "split"
    monkeypatch.setattr(pdf_ops, "MAX_PDF_SPLIT_FILES", 1)

    with pytest.raises(AerError) as caught:
        pdf_ops.split_pdf(source, destination)

    assert caught.value.code == "LIMIT_EXCEEDED"
    assert caught.value.details == {"pages": 2, "limit": 1}
    assert not destination.exists()


def test_split_enforces_aggregate_byte_limit_and_removes_partial_outputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _pdf(tmp_path / "source.pdf", 2)
    destination = tmp_path / "split"
    monkeypatch.setattr(pdf_ops, "MAX_PDF_OUTPUT_BYTES", 600)

    with pytest.raises(AerError) as caught:
        pdf_ops.split_pdf(source, destination)

    assert caught.value.code == "LIMIT_EXCEEDED"
    assert caught.value.details["limit"] == 600
    assert caught.value.details["bytes_attempted"] > 600
    assert not destination.exists()


def test_extract_and_split_keep_source_metadata(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "source.pdf", 2, title="Metadata survives")
    extracted = tmp_path / "extracted.pdf"
    split_dir = tmp_path / "split"

    extract_result = pdf_ops.extract_pdf(source, extracted, pages="2")
    split_result = pdf_ops.split_pdf(source, split_dir)

    assert extract_result["bytes_written"] == extracted.stat().st_size
    assert split_result["bytes_written"] == sum(
        Path(item["output"]).stat().st_size for item in split_result["files"]
    )
    assert PdfReader(extracted).metadata.title == "Metadata survives"
    assert all(
        PdfReader(item["output"]).metadata.title == "Metadata survives"
        for item in split_result["files"]
    )


def test_pdf_input_byte_limit_is_checked_before_parsing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _pdf(tmp_path / "source.pdf", 1)
    monkeypatch.setattr(pdf_safety, "MAX_PDF_INPUT_BYTES", source.stat().st_size - 1)

    with pytest.raises(AerError) as caught:
        pdf_ops.inspect_pdf(source)

    assert caught.value.code == "LIMIT_EXCEEDED"
    assert caught.value.details == {
        "bytes": source.stat().st_size,
        "limit": source.stat().st_size - 1,
    }


def test_pdf_merge_rejects_aggregate_input_bytes_before_opening_readers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = _pdf(tmp_path / "first.pdf", 1)
    second = _pdf(tmp_path / "second.pdf", 1)
    total = first.stat().st_size + second.stat().st_size
    monkeypatch.setattr(pdf_safety, "MAX_PDF_AGGREGATE_INPUT_BYTES", total - 1)

    with pytest.raises(AerError) as caught:
        pdf_ops.merge_pdfs([first, second], tmp_path / "merged.pdf")

    assert caught.value.code == "LIMIT_EXCEEDED"
    assert caught.value.details == {"bytes": total, "limit": total - 1}
    assert not (tmp_path / "merged.pdf").exists()


def test_pdf_text_worker_is_invoked_without_shell_and_with_discarded_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _pdf(tmp_path / "source.pdf", 1)
    observed: dict[str, object] = {}
    payload = {
        "ok": True,
        "result": {
            "page": 1,
            "text": "bounded",
            "line_count": 1,
            "text_bytes": 7,
            "extraction_truncated": False,
        },
    }

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        observed.update({"argv": argv, **kwargs})
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload).encode("utf-8"),
        )

    monkeypatch.setattr(pdf_safety, "_run_subprocess", fake_run)

    result = pdf_safety.extract_pdf_page_text(source, 1, operation="pdf.inspect")

    assert result["text"] == "bounded"
    argv = observed["argv"]
    assert isinstance(argv, list)
    assert argv[0] == pdf_safety.sys.executable
    assert str(argv[1]).endswith("aer/pdf/_text_worker.py")
    assert observed["shell"] is False
    assert observed["stderr"] is subprocess.DEVNULL
    assert observed["timeout"] == pdf_safety.PDF_TEXT_TIMEOUT_SECONDS


def test_pdf_text_worker_timeout_is_a_compact_limit_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _pdf(tmp_path / "source.pdf", 1)

    def time_out(argv: list[str], **kwargs: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(argv, float(kwargs["timeout"]))

    monkeypatch.setattr(pdf_safety, "_run_subprocess", time_out)

    with pytest.raises(AerError) as caught:
        pdf_safety.extract_pdf_page_text(source, 1, operation="pdf.inspect")

    assert caught.value.code == "LIMIT_EXCEEDED"
    assert caught.value.operation == "pdf.inspect"
    assert caught.value.details == {
        "timeout_seconds": pdf_safety.PDF_TEXT_TIMEOUT_SECONDS,
    }
