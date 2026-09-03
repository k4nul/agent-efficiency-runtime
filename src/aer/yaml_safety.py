"""Bounded safe-YAML loading for executable runtime specifications."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import yaml

from aer.errors import AerError

MAX_YAML_ALIASES = 100
MAX_YAML_DEPTH = 100
MAX_YAML_NODES = 100_000


@dataclass(slots=True)
class _Traversal:
    operation: str
    target: str
    nodes: int = 0

    def visit(self, value: Any, *, depth: int, visiting: set[int]) -> None:
        self.nodes += 1
        if self.nodes > MAX_YAML_NODES:
            raise AerError(
                "LIMIT_EXCEEDED",
                "YAML structure exceeds the node safety limit.",
                self.operation,
                self.target,
                {"limit": MAX_YAML_NODES},
            )
        if depth > MAX_YAML_DEPTH:
            raise AerError(
                "LIMIT_EXCEEDED",
                "YAML structure exceeds the depth safety limit.",
                self.operation,
                self.target,
                {"limit": MAX_YAML_DEPTH},
            )
        if value is None or isinstance(value, (str, bool, int)):
            return
        if isinstance(value, float):
            if math.isfinite(value):
                return
            raise AerError(
                "INVALID_SPEC",
                "YAML numeric values must be finite.",
                self.operation,
                self.target,
            )
        if not isinstance(value, (dict, list)):
            raise AerError(
                "INVALID_SPEC",
                "YAML values must use JSON-compatible scalar, array, and object types.",
                self.operation,
                self.target,
                {"type": type(value).__name__},
            )
        identity = id(value)
        if identity in visiting:
            raise AerError(
                "INVALID_SPEC",
                "Cyclic YAML aliases are not supported.",
                self.operation,
                self.target,
            )
        visiting.add(identity)
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise AerError(
                        "INVALID_SPEC",
                        "YAML mapping keys must be strings.",
                        self.operation,
                        self.target,
                        {"type": type(key).__name__},
                    )
                self.visit(key, depth=depth + 1, visiting=visiting)
                self.visit(item, depth=depth + 1, visiting=visiting)
        else:
            for item in value:
                self.visit(item, depth=depth + 1, visiting=visiting)
        visiting.remove(identity)


def load_yaml_safely(text: str, *, operation: str, target: str) -> Any:
    """Load YAML without constructors, alias bombs, cycles, or unbounded trees."""

    try:
        aliases = sum(
            isinstance(event, yaml.events.AliasEvent)
            for event in yaml.parse(text, Loader=yaml.SafeLoader)
        )
        if aliases > MAX_YAML_ALIASES:
            raise AerError(
                "LIMIT_EXCEEDED",
                "YAML alias count exceeds the safety limit.",
                operation,
                target,
                {"aliases": aliases, "limit": MAX_YAML_ALIASES},
            )
        value = yaml.safe_load(text)
    except AerError:
        raise
    except RecursionError as exc:
        raise AerError(
            "LIMIT_EXCEEDED",
            "YAML structure exceeds the parser depth limit.",
            operation,
            target,
            {"limit": MAX_YAML_DEPTH},
        ) from exc
    except yaml.YAMLError as exc:
        raise AerError(
            "INVALID_SPEC",
            "YAML could not be parsed safely.",
            operation,
            target,
            {"error": str(exc).splitlines()[0][:300]},
        ) from exc
    _Traversal(operation, target).visit(value, depth=0, visiting=set())
    return value
