"""Bounded JSON and safe-YAML inspection."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from aer.errors import AerError
from aer.inspect.common import RawSink, preserve_overflow, read_text

_MAX_YAML_ALIASES = 100
_MAX_STRUCTURED_DEPTH = 100
_MAX_STRUCTURED_NODES = 100_000


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
            _preflight_json(text, path)
            value = json.loads(text)
        else:
            _preflight_yaml(text, path)
            value = yaml.safe_load(text)
        _validate_loaded_structure(value, path)
        return value
    except AerError:
        raise
    except (MemoryError, RecursionError) as exc:
        raise AerError(
            "LIMIT_EXCEEDED",
            f"{kind.upper()} exceeds the structured-data safety limits.",
            operation="inspect",
            target=str(path),
            details={
                "max_depth": _MAX_STRUCTURED_DEPTH,
                "max_nodes": _MAX_STRUCTURED_NODES,
            },
        ) from exc
    except (ValueError, yaml.YAMLError) as exc:
        raise AerError(
            "CORRUPT_FILE",
            f"{kind.upper()} could not be parsed safely.",
            operation="inspect",
            target=str(path),
            details={"error": str(exc).splitlines()[0][:300]},
        ) from exc


def _preflight_json(text: str, path: Path) -> None:
    """Reject excessive JSON nesting and value counts before object construction."""

    depth = 0
    nodes = 1
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            nodes += 1
            _enforce_structure_limits(path, depth=depth, nodes=nodes)
        elif character in "]}":
            depth = max(0, depth - 1)
        elif character in ",:":
            nodes += 1
            _enforce_structure_limits(path, depth=depth, nodes=nodes)


def _preflight_yaml(text: str, path: Path) -> None:
    """Count safe-loader events before YAML constructors allocate the value tree."""

    depth = 0
    nodes = 0
    aliases = 0
    for event in yaml.parse(text, Loader=yaml.SafeLoader):
        if isinstance(event, yaml.events.AliasEvent):
            aliases += 1
            nodes += 1
            if aliases > _MAX_YAML_ALIASES:
                raise AerError(
                    "LIMIT_EXCEEDED",
                    "YAML alias count exceeds the safe inspection limit.",
                    operation="inspect",
                    target=str(path),
                    details={"aliases": aliases, "limit": _MAX_YAML_ALIASES},
                )
        elif isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
            depth += 1
            nodes += 1
        elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
            depth = max(0, depth - 1)
        elif isinstance(event, yaml.events.ScalarEvent):
            nodes += 1
        _enforce_structure_limits(path, depth=depth, nodes=nodes)


def _validate_loaded_structure(value: Any, path: Path) -> None:
    """Iteratively bound logical alias expansion while permitting cyclic inspection."""

    nodes = 0
    active: set[int] = set()
    stack: list[tuple[Any, int, bool]] = [(value, 0, False)]
    while stack:
        current, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(current))
            continue
        nodes += 1
        _enforce_structure_limits(path, depth=depth, nodes=nodes)
        if not isinstance(current, (dict, list)):
            continue
        identity = id(current)
        if identity in active:
            continue
        active.add(identity)
        stack.append((current, depth, True))
        if isinstance(current, dict):
            children = [item for pair in current.items() for item in pair]
        else:
            children = list(current)
        stack.extend((child, depth + 1, False) for child in reversed(children))


def _enforce_structure_limits(path: Path, *, depth: int, nodes: int) -> None:
    if depth > _MAX_STRUCTURED_DEPTH:
        raise AerError(
            "LIMIT_EXCEEDED",
            "Structured data exceeds the nesting-depth safety limit.",
            operation="inspect",
            target=str(path),
            details={"depth": depth, "limit": _MAX_STRUCTURED_DEPTH},
        )
    if nodes > _MAX_STRUCTURED_NODES:
        raise AerError(
            "LIMIT_EXCEEDED",
            "Structured data exceeds the node-count safety limit.",
            operation="inspect",
            target=str(path),
            details={"nodes": nodes, "limit": _MAX_STRUCTURED_NODES},
        )


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
