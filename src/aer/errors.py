"""Stable error codes and CLI exit-code mapping."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ERROR_EXIT_CODES: dict[str, int] = {
    "INVALID_ARGUMENT": 2,
    "INVALID_SPEC": 2,
    "INVALID_SELECTOR": 2,
    "INVALID_PATCH": 2,
    "NOT_FOUND": 3,
    "UNSUPPORTED_FORMAT": 4,
    "DEPENDENCY_MISSING": 5,
    "CONFLICT": 6,
    "HASH_MISMATCH": 6,
    "LIMIT_EXCEEDED": 7,
    "COMMAND_FAILED": 8,
    "COMMAND_TIMEOUT": 9,
    "CORRUPT_FILE": 10,
    "VALIDATION_FAILED": 11,
    "PATH_OUTSIDE_ROOT": 12,
    "UNTRUSTED_RECIPE": 13,
    "TEXT_OVERFLOW": 11,
    "INTERNAL_ERROR": 70,
}


@dataclass(slots=True)
class AerError(Exception):
    code: str
    message: str
    operation: str = "unknown"
    target: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    suggested_action: str | None = None
    raw_ref: str | None = None

    @property
    def exit_code(self) -> int:
        return ERROR_EXIT_CODES.get(self.code, 70)


def invalid_argument(message: str, *, operation: str, target: str | None = None) -> AerError:
    return AerError("INVALID_ARGUMENT", message, operation=operation, target=target)
