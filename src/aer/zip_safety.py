"""Fail-fast ZIP expansion limits for untrusted package inputs."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from aer.errors import AerError
from aer.limits import (
    MAX_ZIP_COMPRESSION_RATIO,
    MAX_ZIP_ENTRIES,
    MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES,
    MAX_ZIP_UNCOMPRESSED_BYTES,
)

_RATIO_CHECK_MINIMUM_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ZipSafetyStats:
    """Central-directory values checked without expanding archive members."""

    entries: int
    uncompressed_bytes: int


@dataclass(frozen=True, slots=True)
class ZipActiveContent:
    external_links: tuple[str, ...]
    active_parts: tuple[str, ...]


def enforce_zip_expansion_limits(
    source: str | Path | bytes,
    *,
    operation: str,
    target: str | None = None,
) -> ZipSafetyStats:
    """Reject oversized ZIP packages before a format library expands their parts."""

    archive_source: str | Path | io.BytesIO
    if isinstance(source, bytes):
        archive_source = io.BytesIO(source)
    else:
        archive_source = source
        if target is None:
            target = str(source)

    try:
        with zipfile.ZipFile(archive_source) as archive:
            entries = archive.infolist()
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise AerError(
            "CORRUPT_FILE",
            "ZIP/OOXML package is not a readable ZIP archive.",
            operation=operation,
            target=target,
        ) from exc

    entry_count = len(entries)
    if entry_count > MAX_ZIP_ENTRIES:
        raise AerError(
            "LIMIT_EXCEEDED",
            "ZIP/OOXML package contains too many entries.",
            operation=operation,
            target=target,
            details={"entries": entry_count, "limit": MAX_ZIP_ENTRIES},
        )

    for entry in entries:
        if entry.flag_bits & 0x1:
            raise AerError(
                "UNSUPPORTED_FORMAT",
                "Encrypted ZIP/OOXML entries are not supported.",
                operation=operation,
                target=entry.filename,
            )
        if entry.file_size > MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES:
            raise AerError(
                "LIMIT_EXCEEDED",
                "ZIP/OOXML entry exceeds the per-entry expanded-size limit.",
                operation=operation,
                target=entry.filename,
                details={
                    "uncompressed_bytes": entry.file_size,
                    "limit": MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES,
                },
            )
        if entry.file_size >= _RATIO_CHECK_MINIMUM_BYTES:
            ratio = entry.file_size / max(1, entry.compress_size)
            if ratio > MAX_ZIP_COMPRESSION_RATIO:
                raise AerError(
                    "LIMIT_EXCEEDED",
                    "ZIP/OOXML entry exceeds the compression-ratio safety limit.",
                    operation=operation,
                    target=entry.filename,
                    details={
                        "compression_ratio": round(ratio, 2),
                        "limit": MAX_ZIP_COMPRESSION_RATIO,
                    },
                )

    uncompressed_bytes = sum(entry.file_size for entry in entries)
    if uncompressed_bytes > MAX_ZIP_UNCOMPRESSED_BYTES:
        raise AerError(
            "LIMIT_EXCEEDED",
            "ZIP/OOXML expanded size exceeds the safety limit.",
            operation=operation,
            target=target,
            details={
                "uncompressed_bytes": uncompressed_bytes,
                "limit": MAX_ZIP_UNCOMPRESSED_BYTES,
            },
        )

    return ZipSafetyStats(entries=entry_count, uncompressed_bytes=uncompressed_bytes)


def inspect_zip_active_content(
    source: str | Path | bytes, *, operation: str, target: str | None = None
) -> ZipActiveContent:
    """Identify Office relationships that could fetch or execute external content."""

    enforce_zip_expansion_limits(source, operation=operation, target=target)
    archive_source: str | Path | io.BytesIO = (
        io.BytesIO(source) if isinstance(source, bytes) else source
    )
    external: set[str] = set()
    active: set[str] = set()
    executable_suffixes = (".exe", ".dll", ".com", ".scr", ".bat", ".cmd", ".vbs", ".js")
    try:
        with zipfile.ZipFile(archive_source) as archive:
            for entry in archive.infolist():
                lowered = entry.filename.casefold()
                if lowered.endswith("vbaproject.bin") or lowered.endswith(executable_suffixes):
                    active.add(entry.filename)
                if not lowered.endswith(".rels"):
                    continue
                try:
                    root = ElementTree.fromstring(archive.read(entry))
                except ElementTree.ParseError as exc:
                    raise AerError(
                        "CORRUPT_FILE",
                        "Office relationship XML is malformed.",
                        operation=operation,
                        target=entry.filename,
                    ) from exc
                for relationship in root:
                    if relationship.attrib.get("TargetMode") == "External":
                        external.add(relationship.attrib.get("Target", ""))
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise AerError(
            "CORRUPT_FILE",
            "ZIP/OOXML package is not readable.",
            operation=operation,
            target=target,
        ) from exc
    return ZipActiveContent(tuple(sorted(external)), tuple(sorted(active)))
