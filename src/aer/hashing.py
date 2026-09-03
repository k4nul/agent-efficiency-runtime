"""Hash helpers used for content IDs, cache keys, and patch preconditions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, BinaryIO


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_stream(stream: BinaryIO, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(chunk_size):
        digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return sha256_stream(handle)


def sha256_directory(path: Path) -> str:
    """Hash directory entry names, types, symlink targets, and file contents."""

    root = path.resolve(strict=True)
    digest = hashlib.sha256()
    for item in sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix()):
        relative = item.relative_to(root).as_posix().encode("utf-8")
        if item.is_symlink():
            kind = b"symlink"
            content_hash = str(item.readlink()).encode("utf-8")
        elif item.is_dir():
            kind = b"directory"
            content_hash = b""
        elif item.is_file():
            kind = b"file"
            content_hash = sha256_file(item).encode("ascii")
        else:
            kind = b"other"
            content_hash = b""
        for value in (kind, relative, content_hash):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()


def normalized_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256_bytes(encoded)
