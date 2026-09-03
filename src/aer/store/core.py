"""Content-addressed storage with compact, durable metadata."""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import re
import sqlite3
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Literal

from filelock import FileLock

from aer.config import Settings
from aer.errors import AerError
from aer.limits import DEFAULT_OUTPUT_BYTES, MAX_STDIN_BYTES
from aer.paths import atomic_binary_writer, ensure_regular_input

Namespace = Literal["store", "cache"]

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REF_PATTERN = re.compile(r"^aer://sha256/([0-9a-f]{64})$")
_CHUNK_SIZE = 1024 * 1024
_MAX_SOURCE_METADATA_BYTES = 64 * 1024


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None = None) -> str:
    moment = value or _utc_now()
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def format_ref(digest: str) -> str:
    """Return the canonical AER URI for a lowercase SHA-256 digest."""

    if not _DIGEST_PATTERN.fullmatch(digest):
        raise AerError(
            "INVALID_ARGUMENT",
            "Digest must be exactly 64 lowercase hexadecimal characters.",
            operation="store.ref",
            target=digest,
        )
    return f"aer://sha256/{digest}"


def parse_ref(ref: str, *, operation: str = "store.ref") -> str:
    """Validate an AER URI and return its digest."""

    match = _REF_PATTERN.fullmatch(ref)
    if match is None:
        raise AerError(
            "INVALID_ARGUMENT",
            "Reference must use aer://sha256/ followed by 64 lowercase hex characters.",
            operation=operation,
            target=ref,
        )
    return match.group(1)


@dataclass(frozen=True, slots=True)
class ObjectRecord:
    """Persistent metadata for one stored content object."""

    digest: str
    namespace: Namespace
    filename: str | None
    mime_type: str
    size: int
    created_at: str
    updated_at: str
    accessed_at: str
    source: dict[str, object]
    pinned: bool

    @property
    def ref(self) -> str:
        return format_ref(self.digest)

    def as_dict(self) -> dict[str, object]:
        return {
            "ref": self.ref,
            "digest": self.digest,
            "namespace": self.namespace,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size": self.size,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "accessed_at": self.accessed_at,
            "source": self.source,
            "pinned": self.pinned,
        }


@dataclass(frozen=True, slots=True)
class CatResult:
    """A bounded text selection from a stored object."""

    ref: str
    text: str
    start_line: int
    end_line: int
    returned_lines: int
    truncated: bool
    raw_ref: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "ref": self.ref,
            "text": self.text,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "returned_lines": self.returned_lines,
            "truncated": self.truncated,
            "raw_ref": self.raw_ref,
        }


@dataclass(frozen=True, slots=True)
class GCResult:
    """Summary of a bounded-root garbage-collection pass."""

    scanned: int
    deleted: int
    bytes_reclaimed: int
    sample_refs: tuple[str, ...]
    dry_run: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "deleted": self.deleted,
            "bytes_reclaimed": self.bytes_reclaimed,
            "sample_refs": list(self.sample_refs),
            "dry_run": self.dry_run,
        }


class ObjectStore:
    """A SHA-256 object store backed by atomic files and SQLite metadata.

    Permanent objects and disposable cache objects use physically separate roots
    and metadata namespaces. Incoming file symlinks are rejected. Internal object
    paths are derived only from validated digests and are checked before access.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        namespace: Namespace = "store",
    ) -> None:
        if namespace not in ("store", "cache"):
            raise AerError(
                "INVALID_ARGUMENT",
                "Object namespace must be either 'store' or 'cache'.",
                operation="store.init",
                target=str(namespace),
            )
        self.settings = settings or Settings.load()
        self.settings.ensure()
        self.namespace: Namespace = namespace
        configured_root = (
            self.settings.store_dir if namespace == "store" else self.settings.cache_dir
        )
        self._root = configured_root.expanduser().resolve(strict=True)
        self._objects_root = self._internal_directory(self._root / "sha256")
        self._locks_root = self._internal_directory(self._root / ".locks")
        self._temporary_root = self._internal_directory(self._root / ".tmp")
        self._initialize_database()

    def put_bytes(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        source: Mapping[str, object] | None = None,
        pin: bool = False,
    ) -> ObjectRecord:
        """Store bytes and return deduplicated content metadata."""

        return self.put_stream(
            io.BytesIO(data),
            filename=filename,
            mime_type=mime_type,
            source=source,
            pin=pin,
        )

    def put_file(
        self,
        path: str | Path,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        source: Mapping[str, object] | None = None,
        pin: bool = False,
    ) -> ObjectRecord:
        """Store a regular file without loading it wholly into memory."""

        operation = "store.put"
        resolved = ensure_regular_input(Path(path), operation=operation)
        source_metadata: dict[str, object] = {"path": str(resolved)}
        if source is not None:
            source_metadata.update(source)
        with resolved.open("rb") as handle:
            return self.put_stream(
                handle,
                filename=filename or resolved.name,
                mime_type=mime_type,
                source=source_metadata,
                pin=pin,
            )

    def put_stream(
        self,
        stream: BinaryIO,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        source: Mapping[str, object] | None = None,
        pin: bool = False,
        max_bytes: int | None = None,
    ) -> ObjectRecord:
        """Stream an object into staging, hash it, then atomically publish it."""

        operation = "store.put"
        safe_filename = self._validate_filename(filename, operation=operation)
        safe_mime = self._mime_type(safe_filename, mime_type)
        source_json = self._serialize_source(source, operation=operation)
        if max_bytes is not None and max_bytes < 0:
            raise AerError(
                "INVALID_ARGUMENT",
                "Maximum byte count cannot be negative.",
                operation=operation,
                target=str(max_bytes),
            )

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".incoming-", suffix=".tmp", dir=self._temporary_root
        )
        os.chmod(temporary_name, 0o600)
        temporary_path = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as destination:
                while True:
                    chunk = stream.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise AerError(
                            "INVALID_ARGUMENT",
                            "Input stream must produce bytes.",
                            operation=operation,
                        )
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise AerError(
                            "LIMIT_EXCEEDED",
                            f"Input exceeds the {max_bytes}-byte limit.",
                            operation=operation,
                            details={"limit": max_bytes, "observed_at_least": size},
                        )
                    digest.update(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            return self._publish(
                temporary_path,
                digest.hexdigest(),
                size=size,
                filename=safe_filename,
                mime_type=safe_mime,
                source_json=source_json,
                pin=pin,
            )
        finally:
            temporary_path.unlink(missing_ok=True)

    def put_stdin(
        self,
        *,
        stream: BinaryIO | None = None,
        filename: str | None = None,
        mime_type: str | None = None,
        source: Mapping[str, object] | None = None,
        pin: bool = False,
        max_bytes: int = MAX_STDIN_BYTES,
    ) -> ObjectRecord:
        """Store binary standard input using the configured stdin limit."""

        return self.put_stream(
            stream or sys.stdin.buffer,
            filename=filename,
            mime_type=mime_type,
            source=source,
            pin=pin,
            max_bytes=max_bytes,
        )

    def stat(self, ref: str) -> ObjectRecord:
        """Return metadata while detecting missing or size-corrupt content."""

        operation = "store.stat"
        digest = parse_ref(ref, operation=operation)
        record = self._lookup(digest)
        path = self._object_path(digest)
        if record is None:
            if not path.exists() and not path.is_symlink():
                raise AerError("NOT_FOUND", "Stored object was not found.", operation, ref)
            self._verify_path(path, digest, expected_size=None, operation=operation)
            record = self._upsert_metadata(
                digest,
                size=path.stat().st_size,
                filename=None,
                mime_type="application/octet-stream",
                source_json="{}",
                pin=False,
            )
        else:
            self._verify_path(
                path,
                digest,
                expected_size=record.size,
                operation=operation,
                hash_content=False,
            )
        return record

    def verify(self, ref: str) -> ObjectRecord:
        """Hash stored content and compare it with both URI and metadata."""

        operation = "store.verify"
        digest = parse_ref(ref, operation=operation)
        with self._object_lock(digest):
            record = self.stat(ref)
            self._verify_path(
                self._object_path(digest),
                digest,
                expected_size=record.size,
                operation=operation,
                hash_content=True,
            )
            self._touch(digest)
            return self._required_lookup(digest, operation=operation)

    def get_bytes(self, ref: str) -> bytes:
        """Read a verified object into memory for internal consumers."""

        operation = "store.get"
        digest = parse_ref(ref, operation=operation)
        with self._object_lock(digest):
            record = self._required_or_reconciled(ref, operation=operation)
            path = self._object_path(digest)
            self._verify_path(
                path,
                digest,
                expected_size=record.size,
                operation=operation,
                hash_content=True,
            )
            data = path.read_bytes()
            self._touch(digest)
            return data

    def resolve_path(self, ref: str) -> Path:
        """Return a verified internal path for read-only local consumers."""

        digest = parse_ref(ref, operation="store.resolve")
        self.verify(ref)
        return self._object_path(digest)

    def materialize_path(self, ref: str) -> Path:
        """Create a temporary read-only hard link with the recorded file suffix.

        The caller owns the returned temporary link when it differs from
        :meth:`resolve_path` and must unlink it after use.
        """

        record = self.verify(ref)
        source = self._object_path(record.digest)
        suffix = Path(record.filename or "").suffix.casefold()
        if not suffix or len(suffix) > 16 or not re.fullmatch(r"\.[a-z0-9]+", suffix):
            return source
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".resolved-{record.digest}-", suffix=suffix, dir=self._temporary_root
        )
        os.close(descriptor)
        destination = Path(temporary_name)
        destination.unlink()
        try:
            os.link(source, destination)
        except OSError:
            with source.open("rb") as source_handle, atomic_binary_writer(destination) as handle:
                while chunk := source_handle.read(_CHUNK_SIZE):
                    handle.write(chunk)
        return destination

    def get_to_file(
        self,
        ref: str,
        output: str | Path,
        *,
        overwrite: bool = False,
    ) -> ObjectRecord:
        """Copy verified content to an explicit path using atomic replacement."""

        operation = "store.get"
        digest = parse_ref(ref, operation=operation)
        requested_destination = Path(output).expanduser()
        if requested_destination.is_symlink():
            raise AerError(
                "INVALID_ARGUMENT",
                "Symbolic-link outputs are not accepted.",
                operation=operation,
                target=str(output),
            )
        destination = requested_destination.resolve(strict=False)
        if destination.exists() and not overwrite:
            raise AerError(
                "CONFLICT",
                "Output already exists; pass overwrite to replace it.",
                operation=operation,
                target=str(output),
            )
        source_path = self._object_path(digest)
        if destination == source_path.resolve(strict=False):
            raise AerError(
                "CONFLICT",
                "Output cannot be the object-store source path.",
                operation=operation,
                target=str(output),
            )
        with self._object_lock(digest):
            record = self._required_or_reconciled(ref, operation=operation)
            self._verify_path(
                source_path,
                digest,
                expected_size=record.size,
                operation=operation,
                hash_content=True,
            )
            with (
                source_path.open("rb") as source_handle,
                atomic_binary_writer(destination) as output_handle,
            ):
                while chunk := source_handle.read(_CHUNK_SIZE):
                    output_handle.write(chunk)
            self._touch(digest)
            return self._required_lookup(digest, operation=operation)

    def get(
        self,
        ref: str,
        output: str | Path,
        *,
        overwrite: bool = False,
    ) -> ObjectRecord:
        """Compatibility spelling for the CLI-facing get operation."""

        return self.get_to_file(ref, output, overwrite=overwrite)

    def cat(
        self,
        ref: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        encoding: str = "utf-8",
        max_bytes: int = DEFAULT_OUTPUT_BYTES,
        full: bool = False,
    ) -> CatResult:
        """Return a one-based line selection without accidentally dumping binaries."""

        operation = "store.cat"
        first = 1 if start_line is None else start_line
        if first < 1:
            raise AerError(
                "INVALID_ARGUMENT",
                "start_line must be at least 1.",
                operation=operation,
                target=str(start_line),
            )
        if end_line is not None and end_line < first:
            raise AerError(
                "INVALID_ARGUMENT",
                "end_line must be greater than or equal to start_line.",
                operation=operation,
                target=str(end_line),
            )
        if max_bytes < 1:
            raise AerError(
                "INVALID_ARGUMENT",
                "max_bytes must be at least 1.",
                operation=operation,
                target=str(max_bytes),
            )

        digest = parse_ref(ref, operation=operation)
        selected: list[str] = []
        emitted_bytes = 0
        returned_lines = 0
        last_line = first - 1
        truncated = False
        with self._object_lock(digest):
            record = self._required_or_reconciled(ref, operation=operation)
            path = self._object_path(digest)
            self._verify_path(
                path,
                digest,
                expected_size=record.size,
                operation=operation,
                hash_content=True,
            )
            try:
                with path.open("r", encoding=encoding, errors="strict", newline="") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if line_number < first:
                            continue
                        if end_line is not None and line_number > end_line:
                            break
                        encoded = line.encode("utf-8")
                        if not full and emitted_bytes + len(encoded) > max_bytes:
                            remaining = max_bytes - emitted_bytes
                            if remaining > 0:
                                partial = encoded[:remaining].decode("utf-8", errors="ignore")
                                if partial:
                                    selected.append(partial)
                                    returned_lines += 1
                            truncated = True
                            last_line = line_number
                            break
                        selected.append(line)
                        emitted_bytes += len(encoded)
                        returned_lines += 1
                        last_line = line_number
            except (LookupError, UnicodeDecodeError) as exc:
                raise AerError(
                    "UNSUPPORTED_FORMAT",
                    "Stored object cannot be decoded as the requested text encoding.",
                    operation=operation,
                    target=ref,
                    details={"encoding": encoding},
                ) from exc
            self._touch(digest)
        return CatResult(
            ref=ref,
            text="".join(selected),
            start_line=first,
            end_line=last_line,
            returned_lines=returned_lines,
            truncated=truncated,
            raw_ref=ref if truncated else None,
        )

    def list(self, *, limit: int = 20, offset: int = 0) -> list[ObjectRecord]:
        """List metadata newest-first without reading object bodies."""

        operation = "store.list"
        if limit < 1 or limit > 1000:
            raise AerError(
                "INVALID_ARGUMENT",
                "limit must be between 1 and 1000.",
                operation=operation,
                target=str(limit),
            )
        if offset < 0:
            raise AerError(
                "INVALID_ARGUMENT",
                "offset cannot be negative.",
                operation=operation,
                target=str(offset),
            )
        with self.settings.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM aer_objects
                WHERE namespace = ?
                ORDER BY created_at DESC, digest ASC
                LIMIT ? OFFSET ?
                """,
                (self.namespace, limit, offset),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def pin(self, ref: str, *, pinned: bool = True) -> ObjectRecord:
        """Set or clear garbage-collection protection for an object."""

        operation = "store.pin"
        digest = parse_ref(ref, operation=operation)
        with self._object_lock(digest):
            self._required_or_reconciled(ref, operation=operation)
            now = _timestamp()
            with self.settings.connect() as connection:
                connection.execute(
                    """
                    UPDATE aer_objects
                    SET pinned = ?, updated_at = ?
                    WHERE namespace = ? AND digest = ?
                    """,
                    (int(pinned), now, self.namespace, digest),
                )
            return self._required_lookup(digest, operation=operation)

    def delete(self, ref: str, *, force: bool = False) -> bool:
        """Delete one object inside the configured namespace only."""

        operation = "store.delete"
        digest = parse_ref(ref, operation=operation)
        with self._object_lock(digest):
            record = self._lookup(digest)
            path = self._object_path(digest)
            if record is None and not path.exists() and not path.is_symlink():
                return False
            if record is not None and record.pinned and not force:
                raise AerError(
                    "CONFLICT",
                    "Pinned objects cannot be deleted without force.",
                    operation=operation,
                    target=ref,
                )
            if path.exists() or path.is_symlink():
                if path.is_dir() and not path.is_symlink():
                    raise AerError(
                        "CORRUPT_FILE",
                        "Object path is not a regular file.",
                        operation=operation,
                        target=ref,
                    )
                path.unlink()
            with self.settings.connect() as connection:
                connection.execute(
                    "DELETE FROM aer_objects WHERE namespace = ? AND digest = ?",
                    (self.namespace, digest),
                )
            return True

    def gc(
        self,
        *,
        older_than: timedelta | datetime,
        dry_run: bool = False,
    ) -> GCResult:
        """Delete unpinned old objects, never traversing outside the store root."""

        operation = "store.gc"
        cutoff = self._gc_cutoff(older_than, operation=operation)
        cutoff_text = _timestamp(cutoff)
        with self.settings.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM aer_objects
                WHERE namespace = ? AND pinned = 0 AND accessed_at < ?
                ORDER BY accessed_at ASC, digest ASC
                """,
                (self.namespace, cutoff_text),
            ).fetchall()

        deleted = 0
        reclaimed = 0
        samples: list[str] = []
        for row in rows:
            candidate = self._row_to_record(row)
            if dry_run:
                deleted += 1
                reclaimed += candidate.size
                if len(samples) < 20:
                    samples.append(candidate.ref)
                continue
            with self._object_lock(candidate.digest):
                current = self._lookup(candidate.digest)
                if current is None or current.pinned or current.accessed_at >= cutoff_text:
                    continue
                path = self._object_path(candidate.digest)
                if path.exists() or path.is_symlink():
                    if path.is_dir() and not path.is_symlink():
                        raise AerError(
                            "CORRUPT_FILE",
                            "Object path is not a regular file.",
                            operation=operation,
                            target=current.ref,
                        )
                    path.unlink()
                with self.settings.connect() as connection:
                    connection.execute(
                        "DELETE FROM aer_objects WHERE namespace = ? AND digest = ?",
                        (self.namespace, candidate.digest),
                    )
                deleted += 1
                reclaimed += current.size
                if len(samples) < 20:
                    samples.append(current.ref)
        return GCResult(
            scanned=len(rows),
            deleted=deleted,
            bytes_reclaimed=reclaimed,
            sample_refs=tuple(samples),
            dry_run=dry_run,
        )

    def _initialize_database(self) -> None:
        with self.settings.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS aer_objects (
                    namespace TEXT NOT NULL CHECK(namespace IN ('store', 'cache')),
                    digest TEXT NOT NULL,
                    filename TEXT,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL CHECK(size >= 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    accessed_at TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0, 1)),
                    PRIMARY KEY(namespace, digest)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS aer_objects_gc
                ON aer_objects(namespace, pinned, accessed_at)
                """
            )

    def _internal_directory(self, path: Path) -> Path:
        if path.is_symlink():
            raise AerError(
                "CORRUPT_FILE",
                "Object-store internal directory cannot be a symbolic link.",
                operation="store.init",
                target=str(path),
            )
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self._root):
            raise AerError(
                "PATH_OUTSIDE_ROOT",
                "Object-store internal path escapes AER_HOME.",
                operation="store.init",
                target=str(path),
            )
        return resolved

    def _object_path(self, digest: str) -> Path:
        if not _DIGEST_PATTERN.fullmatch(digest):
            raise AerError(
                "INVALID_ARGUMENT",
                "Invalid object digest.",
                operation="store.path",
                target=digest,
            )
        candidate = self._objects_root / digest[:2] / digest[2:4] / digest
        resolved = candidate.resolve(strict=False)
        if candidate.is_symlink() or not resolved.is_relative_to(self._objects_root):
            raise AerError(
                "CORRUPT_FILE",
                "Object path contains an unsafe symbolic link.",
                operation="store.path",
                target=format_ref(digest),
            )
        return candidate

    def _object_lock(self, digest: str) -> FileLock:
        if not _DIGEST_PATTERN.fullmatch(digest):
            raise AerError(
                "INVALID_ARGUMENT",
                "Invalid object digest.",
                operation="store.lock",
                target=digest,
            )
        return FileLock(self._locks_root / f"{digest}.lock", timeout=30)

    def _publish(
        self,
        staging_path: Path,
        digest: str,
        *,
        size: int,
        filename: str | None,
        mime_type: str,
        source_json: str,
        pin: bool,
    ) -> ObjectRecord:
        destination = self._object_path(digest)
        with self._object_lock(digest):
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            resolved_parent = destination.parent.resolve(strict=True)
            if not resolved_parent.is_relative_to(self._objects_root):
                raise AerError(
                    "PATH_OUTSIDE_ROOT",
                    "Object destination escapes the store root.",
                    operation="store.put",
                    target=format_ref(digest),
                )
            if destination.exists() or destination.is_symlink():
                self._verify_path(
                    destination,
                    digest,
                    expected_size=size,
                    operation="store.put",
                    hash_content=True,
                )
            else:
                os.replace(staging_path, destination)
                os.chmod(destination, 0o600)
                self._fsync_directory(destination.parent)
            return self._upsert_metadata(
                digest,
                size=size,
                filename=filename,
                mime_type=mime_type,
                source_json=source_json,
                pin=pin,
            )

    def _upsert_metadata(
        self,
        digest: str,
        *,
        size: int,
        filename: str | None,
        mime_type: str,
        source_json: str,
        pin: bool,
    ) -> ObjectRecord:
        now = _timestamp()
        with self.settings.connect() as connection:
            connection.execute(
                """
                INSERT INTO aer_objects (
                    namespace, digest, filename, mime_type, size,
                    created_at, updated_at, accessed_at, source_json, pinned
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, digest) DO UPDATE SET
                    filename = COALESCE(excluded.filename, aer_objects.filename),
                    mime_type = CASE
                        WHEN excluded.mime_type = 'application/octet-stream'
                        THEN aer_objects.mime_type
                        ELSE excluded.mime_type
                    END,
                    size = excluded.size,
                    updated_at = excluded.updated_at,
                    accessed_at = excluded.accessed_at,
                    source_json = CASE
                        WHEN excluded.source_json = '{}' THEN aer_objects.source_json
                        ELSE excluded.source_json
                    END,
                    pinned = MAX(aer_objects.pinned, excluded.pinned)
                """,
                (
                    self.namespace,
                    digest,
                    filename,
                    mime_type,
                    size,
                    now,
                    now,
                    now,
                    source_json,
                    int(pin),
                ),
            )
            row = connection.execute(
                "SELECT * FROM aer_objects WHERE namespace = ? AND digest = ?",
                (self.namespace, digest),
            ).fetchone()
        if row is None:
            raise AerError(
                "INTERNAL_ERROR",
                "Object metadata could not be recorded.",
                operation="store.put",
                target=format_ref(digest),
            )
        return self._row_to_record(row)

    def _lookup(self, digest: str) -> ObjectRecord | None:
        with self.settings.connect() as connection:
            row = connection.execute(
                "SELECT * FROM aer_objects WHERE namespace = ? AND digest = ?",
                (self.namespace, digest),
            ).fetchone()
        return None if row is None else self._row_to_record(row)

    def _required_lookup(self, digest: str, *, operation: str) -> ObjectRecord:
        record = self._lookup(digest)
        if record is None:
            raise AerError(
                "NOT_FOUND",
                "Stored object metadata was not found.",
                operation=operation,
                target=format_ref(digest),
            )
        return record

    def _required_or_reconciled(self, ref: str, *, operation: str) -> ObjectRecord:
        digest = parse_ref(ref, operation=operation)
        record = self._lookup(digest)
        if record is not None:
            return record
        path = self._object_path(digest)
        if not path.exists() and not path.is_symlink():
            raise AerError("NOT_FOUND", "Stored object was not found.", operation, ref)
        self._verify_path(path, digest, expected_size=None, operation=operation)
        return self._upsert_metadata(
            digest,
            size=path.stat().st_size,
            filename=None,
            mime_type="application/octet-stream",
            source_json="{}",
            pin=False,
        )

    def _touch(self, digest: str) -> None:
        now = _timestamp()
        with self.settings.connect() as connection:
            connection.execute(
                """
                UPDATE aer_objects SET accessed_at = ?
                WHERE namespace = ? AND digest = ?
                """,
                (now, self.namespace, digest),
            )

    def _row_to_record(self, row: sqlite3.Row) -> ObjectRecord:
        try:
            loaded_source = json.loads(str(row["source_json"]))
        except (json.JSONDecodeError, TypeError) as exc:
            raise AerError(
                "CORRUPT_FILE",
                "Object metadata contains invalid source JSON.",
                operation="store.metadata",
                target=str(row["digest"]),
            ) from exc
        if not isinstance(loaded_source, dict):
            raise AerError(
                "CORRUPT_FILE",
                "Object source metadata must be a JSON object.",
                operation="store.metadata",
                target=str(row["digest"]),
            )
        source: dict[str, object] = {str(key): value for key, value in loaded_source.items()}
        namespace_value = str(row["namespace"])
        namespace: Namespace
        if namespace_value == "store":
            namespace = "store"
        elif namespace_value == "cache":
            namespace = "cache"
        else:
            raise AerError(
                "CORRUPT_FILE",
                "Object metadata contains an invalid namespace.",
                operation="store.metadata",
                target=str(row["digest"]),
            )
        filename_value = row["filename"]
        return ObjectRecord(
            digest=str(row["digest"]),
            namespace=namespace,
            filename=None if filename_value is None else str(filename_value),
            mime_type=str(row["mime_type"]),
            size=int(row["size"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            accessed_at=str(row["accessed_at"]),
            source=source,
            pinned=bool(row["pinned"]),
        )

    @staticmethod
    def _validate_filename(filename: str | None, *, operation: str) -> str | None:
        if filename is None:
            return None
        if (
            not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or "\x00" in filename
        ):
            raise AerError(
                "INVALID_ARGUMENT",
                "filename must be a plain basename without path separators.",
                operation=operation,
                target=filename,
            )
        return filename

    @staticmethod
    def _mime_type(filename: str | None, mime_type: str | None) -> str:
        if mime_type is not None:
            cleaned = mime_type.strip()
            if cleaned and "\n" not in cleaned and "\r" not in cleaned:
                return cleaned
            raise AerError(
                "INVALID_ARGUMENT",
                "MIME type must be a non-empty single-line value.",
                operation="store.put",
                target=mime_type,
            )
        guessed = mimetypes.guess_type(filename or "")[0]
        return guessed or "application/octet-stream"

    @staticmethod
    def _serialize_source(
        source: Mapping[str, object] | None,
        *,
        operation: str,
    ) -> str:
        try:
            encoded = json.dumps(
                dict(source or {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise AerError(
                "INVALID_ARGUMENT",
                "Source metadata must contain JSON-serializable values.",
                operation=operation,
            ) from exc
        if len(encoded.encode("utf-8")) > _MAX_SOURCE_METADATA_BYTES:
            raise AerError(
                "LIMIT_EXCEEDED",
                "Source metadata exceeds the 64 KiB limit.",
                operation=operation,
            )
        return encoded

    @staticmethod
    def _verify_path(
        path: Path,
        digest: str,
        *,
        expected_size: int | None,
        operation: str,
        hash_content: bool = True,
    ) -> None:
        ref = format_ref(digest)
        if path.is_symlink() or not path.exists() or not path.is_file():
            raise AerError(
                "CORRUPT_FILE",
                "Stored object is missing or is not a regular file.",
                operation=operation,
                target=ref,
            )
        actual_size = path.stat().st_size
        if expected_size is not None and actual_size != expected_size:
            raise AerError(
                "CORRUPT_FILE",
                "Stored object size does not match its metadata.",
                operation=operation,
                target=ref,
                details={"expected_size": expected_size, "actual_size": actual_size},
            )
        if hash_content:
            actual_digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(_CHUNK_SIZE):
                    actual_digest.update(chunk)
            observed = actual_digest.hexdigest()
            if observed != digest:
                raise AerError(
                    "CORRUPT_FILE",
                    "Stored object content does not match its SHA-256 reference.",
                    operation=operation,
                    target=ref,
                    details={"expected_sha256": digest, "actual_sha256": observed},
                )

    @staticmethod
    def _gc_cutoff(value: timedelta | datetime, *, operation: str) -> datetime:
        if isinstance(value, timedelta):
            if value.total_seconds() < 0:
                raise AerError(
                    "INVALID_ARGUMENT",
                    "older_than duration cannot be negative.",
                    operation=operation,
                )
            return _utc_now() - value
        if value.tzinfo is None or value.utcoffset() is None:
            raise AerError(
                "INVALID_ARGUMENT",
                "older_than datetime must include a timezone.",
                operation=operation,
            )
        return value.astimezone(UTC)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
