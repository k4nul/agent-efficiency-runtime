"""Serializable benchmark measurements with explicit token-estimate semantics."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final

TOKEN_ESTIMATION_METHOD: Final[str] = "ceil(utf8_bytes/4)"


def estimate_tokens(byte_count: int) -> int:
    """Estimate tokens from measured UTF-8 bytes; never provider billing data."""

    if byte_count < 0:
        raise ValueError("byte_count cannot be negative")
    return math.ceil(byte_count / 4)


@dataclass(frozen=True, slots=True)
class VariantMeasurement:
    name: str
    input_bytes: int
    output_bytes: int
    context_bytes: int
    estimated_tokens: int
    estimation_method: str
    not_provider_billed_tokens: bool
    wall_time_ms: float
    retries: int
    valid: bool
    sha256: str
    details: dict[str, object] = field(default_factory=dict)

    @classmethod
    def measured(
        cls,
        name: str,
        *,
        input_bytes: int,
        output_bytes: int,
        context_bytes: int,
        wall_time_ms: float,
        retries: int = 0,
        valid: bool,
        sha256: str,
        details: dict[str, object] | None = None,
    ) -> VariantMeasurement:
        return cls(
            name=name,
            input_bytes=input_bytes,
            output_bytes=output_bytes,
            context_bytes=context_bytes,
            estimated_tokens=estimate_tokens(context_bytes),
            estimation_method=TOKEN_ESTIMATION_METHOD,
            not_provider_billed_tokens=True,
            wall_time_ms=wall_time_ms,
            retries=retries,
            valid=valid,
            sha256=sha256,
            details=details or {},
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "input_bytes": self.input_bytes,
            "output_bytes": self.output_bytes,
            "context_bytes": self.context_bytes,
            "estimated_tokens": self.estimated_tokens,
            "estimation_method": self.estimation_method,
            "not_provider_billed_tokens": self.not_provider_billed_tokens,
            "wall_time_ms": self.wall_time_ms,
            "retries": self.retries,
            "valid": self.valid,
            "sha256": self.sha256,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario: str
    description: str
    direct: VariantMeasurement | None
    aer: VariantMeasurement | None
    success: bool
    context_bytes_saved: int | None
    estimated_tokens_saved: int | None
    error: dict[str, object] | None = None

    @classmethod
    def compared(
        cls,
        scenario: str,
        description: str,
        direct: VariantMeasurement,
        aer: VariantMeasurement,
    ) -> ScenarioResult:
        return cls(
            scenario=scenario,
            description=description,
            direct=direct,
            aer=aer,
            success=direct.valid and aer.valid,
            context_bytes_saved=direct.context_bytes - aer.context_bytes,
            estimated_tokens_saved=direct.estimated_tokens - aer.estimated_tokens,
        )

    @classmethod
    def failed(
        cls,
        scenario: str,
        description: str,
        *,
        code: str,
        message: str,
    ) -> ScenarioResult:
        return cls(
            scenario=scenario,
            description=description,
            direct=None,
            aer=None,
            success=False,
            context_bytes_saved=None,
            estimated_tokens_saved=None,
            error={"code": code, "message": message},
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "scenario": self.scenario,
            "description": self.description,
            "direct": None if self.direct is None else self.direct.as_dict(),
            "aer": None if self.aer is None else self.aer.as_dict(),
            "success": self.success,
            "context_bytes_saved": self.context_bytes_saved,
            "estimated_tokens_saved": self.estimated_tokens_saved,
            "error": self.error,
            "token_estimate_notice": {
                "estimation_method": TOKEN_ESTIMATION_METHOD,
                "not_provider_billed_tokens": True,
            },
        }


@dataclass(frozen=True, slots=True)
class BenchmarkRun:
    run_id: str
    timestamp: str
    duration_ms: float
    success: bool
    scenarios: tuple[ScenarioResult, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "scenarios": [scenario.as_dict() for scenario in self.scenarios],
            "estimated_tokens": True,
            "estimation_method": TOKEN_ESTIMATION_METHOD,
            "not_provider_billed_tokens": True,
        }
