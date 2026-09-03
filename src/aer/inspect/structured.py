"""Bounded JSON and safe-YAML inspection."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from aer.errors import AerError
from aer.inspect.common import RawSink, preserve_overflow, read_text

_ALIAS_RE = re.compile(r"(?<![\w])\*[A-Za-z0-9_-]+")
_MAX_YAML_ALIASES = 100


def inspect_structured(
    path: Path,
    *,
    kind: str,
    outline: bool,
    selector: str | None,
    query: str | None,
    max_items: int,
    max_depth: int,
    raw_sink: RawSink | None,
) -> dict[str, Any]:
    text, encoding, bytes_read = read_text(path)
    value = _load(text, kind=kind, path=path)
    result: dict[str, Any] = {
        "type": kind,
        "encoding": encoding,
        "bytes": bytes_read,
        "root_type": _value_type(value),
        "summary": _summary(value, max_depth=max_depth),
    }
    if outline:
        entries: list[dict[str, Any]] = []
        _outline(value, "", 0, max_depth, entries, set())
        result["outline"] = entries[:max_items]
        if len(entries) > max_items:
            result["truncated"] = True
            result["raw_ref"] = preserve_overflow(
                entries, raw_sink=raw_sink, name=f"{path.name}.outline.json"
            )
    if selector is not None:
        selected = resolve_pointer(value, selector)
        compact, truncated = compact_value(selected, max_items=max_items, max_depth=max_depth)
        result["selector"] = selector
        result["selection"] = compact
        if truncated:
            result["truncated"] = True
            result["raw_ref"] = _preserve_value(
                selected, raw_sink=raw_sink, name=f"{path.name}.selection.json"
            )
    if query is not None:
        if not query:
            raise AerError(
                "INVALID_ARGUMENT",
                "Structured-data query must not be empty.",
                operation="inspect",
                target=str(path),
            )
        matches: list[dict[str, Any]] = []
        _search(value, query.casefold(), "", 0, max_depth, matches, set())
        result["query"] = query
        result["match_count"] = len(matches)
        result["matches"] = matches[:max_items]
        if len(matches) > max_items:
            result["truncated"] = True
            result["raw_ref"] = preserve_overflow(
                matches, raw_sink=raw_sink, name=f"{path.name}.matches.json"
            )
    return result


def _load(text: str, *, kind: str, path: Path) -> Any:
    try:
        if kind == "json":
            return json.loads(text)
        aliases = len(_ALIAS_RE.findall(text))
        if aliases > _MAX_YAML_ALIASES:
            raise AerError(
                "LIMIT_EXCEEDED",
                "YAML alias count exceeds the safe inspection limit.",
                operation="inspect",
                target=str(path),
                details={"aliases": aliases, "limit": _MAX_YAML_ALIASES},
            )
        return yaml.safe_load(text)
    except AerError:
        raise
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise AerError(
            "CORRUPT_FILE",
            f"{kind.upper()} could not be parsed safely.",
            operation="inspect",
            target=str(path),
            details={"error": str(exc).splitlines()[0][:300]},
        ) from exc


def resolve_pointer(value: Any, pointer: str) -> Any:
    """Resolve RFC 6901 pointers plus `START:END` array tokens.

    Array slices are zero-based, start-inclusive, and end-exclusive. For example,
    ``/items/2:5`` selects three array members.
    """

    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise _invalid_pointer(pointer, "JSON Pointer must be empty or begin with '/'.")
    current = value
    for encoded in pointer[1:].split("/"):
        if re.search(r"~(?![01])", encoded):
            raise _invalid_pointer(pointer, f"Invalid JSON Pointer escape: {encoded}")
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                raise _invalid_pointer(pointer, f"Object key was not found: {token}")
            current = current[token]
        elif isinstance(current, list):
            if ":" in token:
                match = re.fullmatch(r"(\d*):(\d*)", token)
                if match is None:
                    raise _invalid_pointer(pointer, f"Invalid array slice: {token}")
                start = int(match.group(1)) if match.group(1) else 0
                end = int(match.group(2)) if match.group(2) else len(current)
                if start > end:
                    raise _invalid_pointer(pointer, "Array slice start exceeds its end.")
                current = current[start:end]
            else:
                try:
                    if re.fullmatch(r"(?:0|[1-9]\d*)", token) is None:
                        raise ValueError
                    index = int(token)
                    current = current[index]
                except (ValueError, IndexError) as exc:
                    raise _invalid_pointer(pointer, f"Invalid array index: {token}") from exc
        else:
            raise _invalid_pointer(pointer, f"Cannot descend through {_value_type(current)}.")
    return current


def compact_value(value: Any, *, max_items: int, max_depth: int) -> tuple[Any, bool]:
    return _compact(value, max_items=max_items, max_depth=max_depth, depth=0, seen=set())


def _compact(
    value: Any,
    *,
    max_items: int,
    max_depth: int,
    depth: int,
    seen: set[int],
) -> tuple[Any, bool]:
    if not isinstance(value, (dict, list)):
        return value, False
    identity = id(value)
    if identity in seen:
        return {"type": "alias", "value": "<recursive>"}, True
    if depth >= max_depth:
        size = len(value)
        return {"type": _value_type(value), "size": size, "truncated": True}, True
    seen.add(identity)
    truncated = len(value) > max_items
    if isinstance(value, list):
        items: list[Any] = []
        for item in value[:max_items]:
            compact, nested = _compact(
                item,
                max_items=max_items,
                max_depth=max_depth,
                depth=depth + 1,
                seen=seen,
            )
            truncated = truncated or nested
            items.append(compact)
        seen.remove(identity)
        return {"items": items, "total": len(value), "truncated": truncated}, truncated
    items_dict: dict[str, Any] = {}
    for key in list(value)[:max_items]:
        compact, nested = _compact(
            value[key],
            max_items=max_items,
            max_depth=max_depth,
            depth=depth + 1,
            seen=seen,
        )
        truncated = truncated or nested
        items_dict[str(key)] = compact
    seen.remove(identity)
    return {"items": items_dict, "total": len(value), "truncated": truncated}, truncated


def _summary(value: Any, *, max_depth: int) -> dict[str, Any]:
    if isinstance(value, dict):
        return {
            "keys": len(value),
            "key_preview": [str(key) for key in list(value)[:20]],
            "depth": _depth(value, max_depth=max_depth, seen=set()),
        }
    if isinstance(value, list):
        return {
            "items": len(value),
            "item_types": sorted({_value_type(item) for item in value[:100]}),
            "depth": _depth(value, max_depth=max_depth, seen=set()),
        }
    return {"value_type": _value_type(value)}


def _depth(value: Any, *, max_depth: int, seen: set[int], current: int = 0) -> int:
    if not isinstance(value, (dict, list)) or current >= max_depth:
        return current
    identity = id(value)
    if identity in seen:
        return current
    seen.add(identity)
    children = value.values() if isinstance(value, dict) else value
    result = max(
        (_depth(child, max_depth=max_depth, seen=seen, current=current + 1) for child in children),
        default=current,
    )
    seen.remove(identity)
    return result


def _outline(
    value: Any,
    path: str,
    depth: int,
    max_depth: int,
    entries: list[dict[str, Any]],
    seen: set[int],
) -> None:
    if depth >= max_depth or not isinstance(value, (dict, list)):
        return
    identity = id(value)
    if identity in seen:
        return
    seen.add(identity)
    iterator = value.items() if isinstance(value, dict) else enumerate(value)
    for key, child in iterator:
        token = str(key).replace("~", "~0").replace("/", "~1")
        child_path = f"{path}/{token}"
        entry: dict[str, Any] = {"path": child_path, "type": _value_type(child)}
        if isinstance(child, (dict, list)):
            entry["size"] = len(child)
        entries.append(entry)
        _outline(child, child_path, depth + 1, max_depth, entries, seen)
    seen.remove(identity)


def _search(
    value: Any,
    query: str,
    path: str,
    depth: int,
    max_depth: int,
    matches: list[dict[str, Any]],
    seen: set[int],
) -> None:
    if depth > max_depth:
        return
    if isinstance(value, (dict, list)):
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        iterator = value.items() if isinstance(value, dict) else enumerate(value)
        for key, child in iterator:
            token = str(key).replace("~", "~0").replace("/", "~1")
            child_path = f"{path}/{token}"
            if query in str(key).casefold():
                matches.append(
                    {"path": child_path, "match": "key", "value_type": _value_type(child)}
                )
            _search(child, query, child_path, depth + 1, max_depth, matches, seen)
        seen.remove(identity)
    elif query in str(value).casefold():
        matches.append({"path": path, "match": "value", "value": value})


def _preserve_value(value: Any, *, raw_sink: RawSink | None, name: str) -> str | None:
    if raw_sink is None:
        return None
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    except ValueError:
        encoded = json.dumps(
            {"value": str(value)}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    return raw_sink(encoded, name)


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


def _invalid_pointer(pointer: str, message: str) -> AerError:
    return AerError(
        "INVALID_SELECTOR",
        message,
        operation="inspect",
        target=pointer,
    )
