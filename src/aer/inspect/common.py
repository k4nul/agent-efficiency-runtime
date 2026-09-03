"""Shared bounded-inspection and target-resolution helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from copy import deepcopy
from pathlib import Path
from typing import Any, TypeAlias

import regex as regex_engine

from aer.errors import AerError
from aer.limits import DEFAULT_OUTPUT_BYTES, MAX_TEXT_FILE_BYTES

TargetResolver: TypeAlias = Callable[[str], str | Path]
RawSink: TypeAlias = Callable[[bytes, str], str]

_SECRET_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secret.yaml",
    "secret.yml",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
}
_SECRET_SUFFIXES = {".key", ".p12", ".pfx", ".pem"}
_REGEX_NESTED_QUANTIFIER = re.compile(r"\([^)]*[+*][^)]*\)\s*(?:[+*]|\{)")
_REGEX_REPEATED_ALTERNATION = re.compile(r"\([^)]*\|[^)]*\)\s*(?:[+*]|\{)")
_REGEX_BACKREFERENCE = re.compile(r"\\[1-9]")


def resolve_target(target: str | Path, resolver: TargetResolver | None) -> Path:
    raw = str(target)
    if raw.startswith("aer://"):
        if resolver is None:
            raise AerError(
                "INVALID_ARGUMENT",
                "An object-store resolver is required for aer:// targets.",
                operation="inspect",
                target=raw,
            )
        resolved_value = resolver(raw)
        path = Path(resolved_value).expanduser()
    else:
        path = Path(target).expanduser()
    if path.is_symlink():
        raise AerError(
            "INVALID_ARGUMENT",
            "Symbolic-link inspection targets are not accepted.",
            operation="inspect",
            target=raw,
        )
    path = path.resolve(strict=False)
    if not path.exists():
        raise AerError("NOT_FOUND", "Inspection target does not exist.", "inspect", raw)
    if not path.is_file() and not path.is_dir():
        raise AerError(
            "INVALID_ARGUMENT",
            "Inspection target must be a regular file or directory.",
            "inspect",
            raw,
        )
    return path


def is_sensitive_path(path: Path) -> bool:
    name = path.name.casefold()
    if (
        name in _SECRET_NAMES
        or name.startswith(".env.")
        or name.startswith(("secret.", "secrets."))
        or path.suffix.casefold() in _SECRET_SUFFIXES
    ):
        return True
    return any(fragment in name for fragment in ("credential", "private-key", "private_key"))


def read_text(path: Path) -> tuple[str, str, int]:
    size = path.stat().st_size
    if size > MAX_TEXT_FILE_BYTES:
        raise AerError(
            "LIMIT_EXCEEDED",
            "Text input exceeds the inspection size limit.",
            operation="inspect",
            target=str(path),
            details={"bytes": size, "limit": MAX_TEXT_FILE_BYTES},
        )
    data = path.read_bytes()
    if b"\x00" in data[:8192] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise AerError(
            "UNSUPPORTED_FORMAT",
            "Binary content is not returned by text inspection.",
            operation="inspect",
            target=str(path),
        )
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig", len(data)
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16"), "utf-16", len(data)
        except UnicodeDecodeError as exc:
            raise _decode_error(path, exc) from exc
    try:
        return data.decode("utf-8"), "utf-8", len(data)
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252"), "cp1252", len(data)
        except UnicodeDecodeError as exc:
            raise _decode_error(path, exc) from exc


def _decode_error(path: Path, error: UnicodeDecodeError) -> AerError:
    return AerError(
        "CORRUPT_FILE",
        "Text encoding could not be decoded safely.",
        operation="inspect",
        target=str(path),
        details={"offset": error.start},
    )


def safe_regex(pattern: str, *, case_sensitive: bool = False) -> Any:
    if not pattern or len(pattern) > 256:
        raise AerError(
            "INVALID_ARGUMENT",
            "Regular expression must contain 1 to 256 characters.",
            operation="inspect",
            target=pattern[:80],
        )
    if (
        _REGEX_BACKREFERENCE.search(pattern)
        or _REGEX_NESTED_QUANTIFIER.search(pattern)
        or _REGEX_REPEATED_ALTERNATION.search(pattern)
    ):
        raise AerError(
            "INVALID_ARGUMENT",
            "Regular expression contains a disallowed high-cost construct.",
            operation="inspect",
            target=pattern[:80],
        )
    try:
        return regex_engine.compile(pattern, 0 if case_sensitive else regex_engine.IGNORECASE)
    except regex_engine.error as exc:
        raise AerError(
            "INVALID_ARGUMENT",
            "Regular expression is invalid.",
            operation="inspect",
            target=pattern[:80],
            details={"error": str(exc)},
        ) from exc


def line_matches(
    line: str,
    query: str,
    *,
    regex: Any | None,
    case_sensitive: bool,
) -> bool:
    if regex is not None:
        try:
            return regex.search(line, timeout=0.02) is not None
        except TimeoutError as exc:
            raise AerError(
                "LIMIT_EXCEEDED",
                "Regular expression execution exceeded the safety timeout.",
                operation="inspect",
                target=str(regex.pattern)[:80],
                suggested_action="Use a literal query or a simpler bounded regular expression.",
            ) from exc
    if case_sensitive:
        return query in line
    return query.casefold() in line.casefold()


def compact_line(value: str, *, limit: int = 500) -> str:
    value = value.rstrip("\r\n")
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def parse_inclusive_range(value: str, *, target: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*:\s*(\d+)\s*", value)
    if match is None:
        raise AerError(
            "INVALID_SELECTOR",
            "Range must use START:END with one-based inclusive values.",
            operation="inspect",
            target=target,
        )
    start, end = (int(part) for part in match.groups())
    if start < 1 or end < start:
        raise AerError(
            "INVALID_SELECTOR",
            "Range start must be at least 1 and not exceed its end.",
            operation="inspect",
            target=target,
        )
    return start, end


def preserve_overflow(
    records: Iterable[Any],
    *,
    raw_sink: RawSink | None,
    name: str,
) -> str | None:
    if raw_sink is None:
        return None
    encoded = json.dumps(
        list(records), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return raw_sink(encoded, name)


def enforce_output_budget(
    result: dict[str, Any],
    *,
    raw_sink: RawSink | None,
    name: str,
    limit: int = DEFAULT_OUTPUT_BYTES - 1024,
) -> dict[str, Any]:
    """Bound the JSON payload while preserving the pre-compaction result when possible."""

    encoded = _json_bytes(result)
    if len(encoded) <= limit:
        return result
    raw_ref = raw_sink(encoded, name) if raw_sink is not None else None
    for item_limit in (10, 5, 2, 1):
        compacted = _cap_containers(deepcopy(result), item_limit=item_limit, depth=0)
        if not isinstance(compacted, dict):
            break
        compacted["truncated"] = True
        compacted["raw_ref"] = raw_ref
        compacted["uncompacted_output_bytes"] = len(encoded)
        if len(_json_bytes(compacted)) <= limit:
            return compacted
    return {
        "type": result.get("type"),
        "target": result.get("target"),
        "truncated": True,
        "raw_ref": raw_ref,
        "uncompacted_output_bytes": len(encoded),
    }


def _cap_containers(value: Any, *, item_limit: int, depth: int) -> Any:
    if isinstance(value, list):
        return [
            _cap_containers(item, item_limit=item_limit, depth=depth + 1)
            for item in value[:item_limit]
        ]
    if isinstance(value, dict):
        items = list(value.items())
        if depth > 0:
            items = items[: max(10, item_limit * 4)]
        return {
            key: _cap_containers(item, item_limit=item_limit, depth=depth + 1)
            for key, item in items
        }
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def validate_limits(*, max_items: int, context: int, full: bool) -> int:
    upper = 10_000 if full else 100
    if max_items < 1 or max_items > upper:
        raise AerError(
            "INVALID_ARGUMENT",
            f"max_items must be between 1 and {upper}.",
            operation="inspect",
            target=str(max_items),
        )
    if context < 0 or context > 20:
        raise AerError(
            "INVALID_ARGUMENT",
            "context must be between 0 and 20 lines.",
            operation="inspect",
            target=str(context),
        )
    return max_items
