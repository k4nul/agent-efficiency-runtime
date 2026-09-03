from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import pathspec

from aer.errors import AerError
from aer.hashing import sha256_file
from aer.limits import (
    MAX_SPEC_FILE_BYTES,
    MAX_ZIP_ENTRIES,
    MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES,
    MAX_ZIP_UNCOMPRESSED_BYTES,
)
from aer.paths import atomic_binary_writer, ensure_regular_input, prepare_output_path
from aer.zip_safety import enforce_zip_expansion_limits

FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def _safe_name(name: str, operation: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise AerError(
            "PATH_OUTSIDE_ROOT", "Archive entry escapes the archive root.", operation, name
        )


def _reject_duplicate_names(entries: list[zipfile.ZipInfo], operation: str) -> None:
    names = [entry.filename for entry in entries]
    duplicates = sorted(name for name in set(names) if names.count(name) > 1)
    if duplicates:
        raise AerError(
            "CORRUPT_FILE",
            "Archive contains duplicate entry names.",
            operation,
            duplicates[0],
            {"duplicates": duplicates[:20]},
        )


def _files(source: Path, excludes: list[str], output: Path) -> list[tuple[str, Path]]:
    matcher = pathspec.PathSpec.from_lines("gitwildmatch", excludes)
    if source.is_symlink():
        raise AerError(
            "INVALID_ARGUMENT",
            "Archive source cannot be a symbolic link.",
            "archive.create",
            str(source),
        )
    if source.is_file():
        name = source.name
        _safe_name(name, "archive.create")
        if name == "manifest.json":
            raise AerError(
                "CONFLICT",
                "manifest.json is reserved for the archive manifest.",
                "archive.create",
                name,
            )
        return [(name, source)]
    if not source.is_dir():
        raise AerError("NOT_FOUND", "Archive source does not exist.", "archive.create", str(source))
    entries: list[tuple[str, Path]] = []
    for path in source.rglob("*"):
        if path.is_symlink():
            raise AerError(
                "INVALID_ARGUMENT",
                "Archive source contains a symbolic link.",
                "archive.create",
                str(path),
            )
        if not path.is_file() or path.resolve() == output.resolve(strict=False):
            continue
        relative = path.relative_to(source).as_posix()
        _safe_name(relative, "archive.create")
        if relative == "manifest.json":
            raise AerError(
                "CONFLICT",
                "manifest.json is reserved for the archive manifest.",
                "archive.create",
                relative,
            )
        if matcher.match_file(relative):
            continue
        entries.append((relative, path))
    return sorted(entries)


def create_archive(
    source: Path,
    output: Path,
    *,
    excludes: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    requested_source = source.expanduser()
    if requested_source.is_symlink():
        raise AerError(
            "INVALID_ARGUMENT",
            "Archive source cannot be a symbolic link.",
            "archive.create",
            str(requested_source),
        )
    source = requested_source.resolve(strict=False)
    output = prepare_output_path(output, operation="archive.create")
    if output.suffix.lower() != ".zip":
        raise AerError(
            "INVALID_ARGUMENT", "Archive output must use .zip.", "archive.create", str(output)
        )
    entries = _files(source, excludes or [], output)
    if len(entries) + 1 > MAX_ZIP_ENTRIES:
        raise AerError(
            "LIMIT_EXCEEDED", "Archive entry count exceeds the safety limit.", "archive.create"
        )
    manifest_files = []
    total = 0
    records: list[tuple[str, Path, int, str]] = []
    for name, path in entries:
        size = path.stat().st_size
        if size > MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES:
            raise AerError(
                "LIMIT_EXCEEDED",
                "Archive input exceeds the per-entry size limit.",
                "archive.create",
                name,
                {"bytes": size, "limit": MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES},
            )
        total += size
        if total > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise AerError(
                "LIMIT_EXCEEDED",
                "Archive expanded size exceeds the safety limit.",
                "archive.create",
            )
        digest = sha256_file(path)
        manifest_files.append({"path": name, "sha256": digest, "size": size})
        records.append((name, path, size, digest))
    manifest_data = (
        json.dumps(
            {
                "version": 1,
                "deterministic_timestamp": "1980-01-01T00:00:00Z",
                "files": manifest_files,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if total + len(manifest_data) > MAX_ZIP_UNCOMPRESSED_BYTES:
        raise AerError(
            "LIMIT_EXCEEDED",
            "Archive manifest would exceed the expanded-size safety limit.",
            "archive.create",
        )
    if dry_run:
        return {
            "dry_run": True,
            "source": str(source),
            "output": str(output),
            "entries": len(records) + 1,
            "uncompressed_bytes": total + len(manifest_data),
        }
    with (
        atomic_binary_writer(output) as destination,
        zipfile.ZipFile(
            destination,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive,
    ):
        for name, path, expected_size, expected_digest in records:
            hasher = hashlib.sha256()
            written = 0
            info = _zip_info(name)
            info.file_size = expected_size
            with (
                path.open("rb") as source_handle,
                archive.open(info, "w") as archive_handle,
            ):
                while chunk := source_handle.read(1024 * 1024):
                    written += len(chunk)
                    hasher.update(chunk)
                    archive_handle.write(chunk)
            if written != expected_size or hasher.hexdigest() != expected_digest:
                raise AerError(
                    "CONFLICT",
                    "Archive input changed while it was being packaged.",
                    "archive.create",
                    str(path),
                )
        archive.writestr(
            _zip_info("manifest.json"),
            manifest_data,
            compress_type=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        )
    return {
        "output": str(output),
        "entries": len(records) + 1,
        "uncompressed_bytes": total + len(manifest_data),
        "sha256": sha256_file(output),
        "deterministic": True,
    }


def _open_archive(path: Path, operation: str) -> tuple[Path, zipfile.ZipFile]:
    source = ensure_regular_input(path, operation=operation)
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise AerError("CORRUPT_FILE", f"Cannot open ZIP: {exc}", operation, str(source)) from exc
    return source, archive


def list_archive(path: Path, *, limit: int = 20) -> dict[str, Any]:
    source, archive = _open_archive(path, "archive.list")
    enforce_zip_expansion_limits(source, operation="archive.list", target=str(source))
    with archive:
        entries = archive.infolist()
        _reject_duplicate_names(entries, "archive.list")
        for item in entries:
            _safe_name(item.filename, "archive.list")
        return {
            "path": str(source),
            "entry_count": len(entries),
            "entries": [
                {
                    "path": item.filename,
                    "size": item.file_size,
                    "compressed_size": item.compress_size,
                }
                for item in entries[:limit]
            ],
            "truncated": len(entries) > limit,
        }


def verify_archive(path: Path) -> dict[str, Any]:
    source, archive = _open_archive(path, "archive.verify")
    enforce_zip_expansion_limits(source, operation="archive.verify", target=str(source))
    with archive:
        entries = archive.infolist()
        _reject_duplicate_names(entries, "archive.verify")
        if len(entries) > MAX_ZIP_ENTRIES:
            raise AerError(
                "LIMIT_EXCEEDED",
                "Archive entry count exceeds the safety limit.",
                "archive.verify",
                str(source),
            )
        total = sum(item.file_size for item in entries)
        if total > MAX_ZIP_UNCOMPRESSED_BYTES:
            raise AerError(
                "LIMIT_EXCEEDED",
                "Archive expanded size exceeds the safety limit.",
                "archive.verify",
                str(source),
            )
        for item in entries:
            _safe_name(item.filename, "archive.verify")
        manifest_entry = next((item for item in entries if item.filename == "manifest.json"), None)
        if manifest_entry is None or manifest_entry.file_size > MAX_SPEC_FILE_BYTES:
            raise AerError(
                "CORRUPT_FILE",
                "Archive manifest is missing or exceeds the manifest size limit.",
                "archive.verify",
                str(source),
            )
        try:
            with archive.open(manifest_entry) as handle:
                manifest = json.loads(handle.read(MAX_SPEC_FILE_BYTES + 1))
        except (KeyError, json.JSONDecodeError, RuntimeError, zipfile.BadZipFile) as exc:
            raise AerError(
                "CORRUPT_FILE",
                "Archive manifest is missing or invalid.",
                "archive.verify",
                str(source),
            ) from exc
        manifest_files = manifest.get("files", [])
        if not isinstance(manifest_files, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("size"), int)
            or not isinstance(item.get("sha256"), str)
            for item in manifest_files
        ):
            raise AerError(
                "CORRUPT_FILE", "Archive manifest file records are invalid.", "archive.verify"
            )
        manifest_names = [item["path"] for item in manifest_files]
        if len(set(manifest_names)) != len(manifest_names):
            raise AerError(
                "CORRUPT_FILE", "Archive manifest contains duplicate paths.", "archive.verify"
            )
        expected_names = set(manifest_names)
        actual_names = {item.filename for item in entries} - {"manifest.json"}
        if expected_names != actual_names:
            raise AerError(
                "HASH_MISMATCH",
                "Archive manifest entries do not match ZIP entries.",
                "archive.verify",
                str(source),
            )
        for record in manifest_files:
            digest = hashlib.sha256()
            size = 0
            try:
                with archive.open(record["path"]) as handle:
                    while chunk := handle.read(1024 * 1024):
                        size += len(chunk)
                        digest.update(chunk)
            except (KeyError, RuntimeError, zipfile.BadZipFile) as exc:
                raise AerError(
                    "CORRUPT_FILE",
                    "Archive entry could not be decompressed safely.",
                    "archive.verify",
                    record["path"],
                ) from exc
            if size != record["size"] or digest.hexdigest() != record["sha256"]:
                raise AerError(
                    "HASH_MISMATCH",
                    "Archive entry does not match its manifest hash.",
                    "archive.verify",
                    record["path"],
                )
    return {
        "valid": True,
        "path": str(source),
        "entries": len(entries),
        "uncompressed_bytes": total,
        "sha256": sha256_file(source),
    }
