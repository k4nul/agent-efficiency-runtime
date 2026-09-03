"""Safe path and atomic-write primitives."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO

from aer.errors import AerError


def ensure_regular_input(path: Path, *, operation: str) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    if not resolved.exists():
        raise AerError("NOT_FOUND", "Input does not exist.", operation, target=str(path))
    if resolved.is_symlink() or path.is_symlink():
        raise AerError(
            "INVALID_ARGUMENT",
            "Symbolic-link inputs are not accepted for this operation.",
            operation,
            target=str(path),
        )
    if not resolved.is_file():
        raise AerError("INVALID_ARGUMENT", "Input must be a regular file.", operation, str(path))
    return resolved


def safe_relative_path(name: str, *, operation: str) -> Path:
    candidate = Path(name)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AerError("PATH_OUTSIDE_ROOT", "Path escapes the allowed root.", operation, name)
    return candidate


def prepare_output_path(path: Path, *, operation: str) -> Path:
    """Resolve a caller-selected output without following a final symlink."""

    requested = path.expanduser()
    if requested.is_symlink():
        raise AerError(
            "INVALID_ARGUMENT",
            "Output cannot be a symbolic link.",
            operation,
            str(requested),
        )
    return requested.resolve(strict=False)


@contextmanager
def atomic_binary_writer(destination: Path) -> Iterator[BinaryIO]:
    destination = prepare_output_path(destination, operation="file.write")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.chmod(temporary_name, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise


def atomic_write_bytes(destination: Path, data: bytes) -> None:
    with atomic_binary_writer(destination) as handle:
        handle.write(data)


def atomic_write_text(destination: Path, text: str) -> None:
    atomic_write_bytes(destination, text.encode("utf-8"))
