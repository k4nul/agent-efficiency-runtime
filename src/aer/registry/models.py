"""Typed capability metadata used by local discovery and schema lookup."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

Risk = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class Capability:
    """A deterministic, locally described runtime operation."""

    name: str
    summary: str
    keywords: tuple[str, ...]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    examples: tuple[dict[str, Any], ...] = ()
    requires: tuple[str, ...] = ()
    risk: Risk = "low"
    operations: tuple[str, ...] = ()
    guidance: dict[str, Any] | None = None
    version: int = 1

    def discovery_record(self) -> dict[str, str]:
        return {"name": self.name, "summary": self.summary}

    def schema_record(self, *, compact: bool, include_example: bool) -> dict[str, Any]:
        if compact:
            properties = self.input_schema.get("properties", {})
            required = set(self.input_schema.get("required", []))
            record: dict[str, Any] = {
                "name": self.name,
                "summary": self.summary,
                "required": {
                    key: _compact_property(value)
                    for key, value in properties.items()
                    if key in required
                },
                "optional": {
                    key: _compact_property(value)
                    for key, value in properties.items()
                    if key not in required
                },
            }
            if self.operations:
                record["operations"] = list(self.operations)
            if self.guidance:
                record["guidance"] = deepcopy(self.guidance)
        else:
            record = {
                "name": self.name,
                "summary": self.summary,
                "keywords": list(self.keywords),
                "input_schema": deepcopy(self.input_schema),
                "output_schema": deepcopy(self.output_schema),
                "requires": list(self.requires),
                "risk": self.risk,
                "version": self.version,
            }
            if self.operations:
                record["operations"] = list(self.operations)
            if self.guidance:
                record["guidance"] = deepcopy(self.guidance)
        if include_example and self.examples:
            record["example"] = deepcopy(self.examples[0])
        if self.requires:
            record["requires"] = list(self.requires)
        record["risk"] = self.risk
        return record


def _compact_property(value: Any) -> dict[str, Any] | Any:
    if not isinstance(value, dict):
        return value
    keep = ("type", "description", "enum", "default", "format", "items")
    return {key: deepcopy(value[key]) for key in keep if key in value}


def object_schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    """Create the small JSON schemas used by the built-in catalog."""

    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


EMPTY_OUTPUT = object_schema({"result": {"type": "object"}})
