"""Bounded, format-aware inspection public API."""

from aer.inspect.common import RawSink, TargetResolver
from aer.inspect.engine import Inspector, inspect_target
from aer.inspect.structured import compact_value, resolve_pointer

__all__ = [
    "Inspector",
    "RawSink",
    "TargetResolver",
    "compact_value",
    "inspect_target",
    "resolve_pointer",
]
