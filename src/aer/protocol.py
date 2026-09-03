"""Compact, stable response protocol shared by every command."""

from __future__ import annotations

import json
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, TypeVar

from aer.errors import ERROR_EXIT_CODES, AerError
from aer.limits import DEFAULT_OUTPUT_BYTES, DISCOVER_OUTPUT_BYTES, SCHEMA_OUTPUT_BYTES

T = TypeVar("T")


@dataclass(slots=True)
class Metrics:
    duration_ms: int = 0
    bytes_read: int = 0
    bytes_written: int = 0
    cache_hit: bool = False


def success(
    operation: str,
    result: dict[str, Any] | None = None,
    *,
    artifacts: list[dict[str, Any]] | None = None,
    warnings: list[dict[str, Any] | str] | None = None,
    metrics: Metrics | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "operation": operation,
        "result": result or {},
        "artifacts": artifacts or [],
        "warnings": warnings or [],
        "metrics": asdict(metrics or Metrics()),
    }


def failure(error: AerError) -> dict[str, Any]:
    return {
        "ok": False,
        "operation": error.operation,
        "code": error.code,
        "message": error.message,
        "target": error.target,
        "details": error.details,
        "suggested_action": error.suggested_action,
        "raw_ref": error.raw_ref,
    }


def render(payload: dict[str, Any], *, pretty: bool = False, human: bool = False) -> str:
    if human:
        if payload.get("ok"):
            operation = payload.get("operation", "operation")
            result = payload.get("result", {})
            return f"OK {operation}: {json.dumps(result, ensure_ascii=False, default=str)}"
        return f"ERROR {payload.get('code')}: {payload.get('message')}"
    separators = None if pretty else (",", ":")
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2 if pretty else None,
        separators=separators,
        sort_keys=False,
        default=str,
    )


def emit(payload: dict[str, Any], *, pretty: bool = False, human: bool = False) -> None:
    sys.stdout.write(render(payload, pretty=pretty, human=human) + "\n")


def _small_result_summary(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"result_type": type(result).__name__}
    summary: dict[str, Any] = {}
    for key, value in list(result.items())[:16]:
        if value is None or isinstance(value, (bool, int, float)):
            summary[key] = value
        elif isinstance(value, str):
            if len(value.encode("utf-8")) <= 512:
                summary[key] = value
            else:
                summary[f"{key}_bytes"] = len(value.encode("utf-8"))
        elif isinstance(value, list):
            small_items: list[Any] = []
            used = 0
            for item in value[:5]:
                encoded = json.dumps(item, ensure_ascii=False, default=str).encode("utf-8")
                if len(encoded) > 512 or used + len(encoded) > 2048:
                    break
                small_items.append(item)
                used += len(encoded)
            if small_items:
                summary[key] = small_items
            if len(small_items) < len(value):
                summary[f"{key}_count"] = len(value)
        elif isinstance(value, dict):
            summary[f"{key}_keys"] = [str(item) for item in list(value)[:20]]
        else:
            summary[f"{key}_type"] = type(value).__name__
    return summary


def _bounded_payload(
    payload: dict[str, Any], *, pretty: bool, human: bool, full: bool
) -> dict[str, Any]:
    operation = str(payload.get("operation", "response"))
    output_budget = {
        "discover": DISCOVER_OUTPUT_BYTES,
        "schema": SCHEMA_OUTPUT_BYTES,
    }.get(operation, DEFAULT_OUTPUT_BYTES)
    rendered = render(payload, pretty=pretty, human=human)
    if full or len(rendered.encode("utf-8")) <= output_budget:
        return payload

    from aer.config import Settings
    from aer.store import ObjectStore

    raw = render(payload).encode("utf-8")
    raw_ref = (
        ObjectStore(Settings.load())
        .put_bytes(
            raw,
            filename=f"{operation.replace('.', '-')}-response.json",
            mime_type="application/json",
            source={"operation": operation, "kind": "full_response"},
        )
        .ref
    )
    if payload.get("ok"):
        artifacts = [item for item in payload.get("artifacts", [])[:10] if isinstance(item, dict)]
        artifacts.append({"ref": raw_ref, "role": "raw_response"})
        compact = success(
            operation,
            {
                **_small_result_summary(payload.get("result")),
                "truncated": True,
                "original_bytes": len(raw),
                "raw_ref": raw_ref,
            },
            artifacts=artifacts,
            warnings=[
                {
                    "code": "OUTPUT_TRUNCATED",
                    "message": "Full response was stored in the content-addressed store.",
                    "warning_count": len(payload.get("warnings", [])),
                }
            ],
            metrics=Metrics(**payload.get("metrics", {})),
        )
    else:
        compact = {
            "ok": False,
            "operation": operation,
            "code": payload.get("code", "INTERNAL_ERROR"),
            "message": str(payload.get("message", ""))[:1024],
            "target": str(payload["target"])[:1024] if payload.get("target") else None,
            "details": {
                **_small_result_summary(payload.get("details")),
                "truncated": True,
                "original_bytes": len(raw),
            },
            "suggested_action": payload.get("suggested_action"),
            "raw_ref": raw_ref,
        }
    if len(render(compact, pretty=pretty, human=human).encode("utf-8")) > output_budget:
        if payload.get("ok"):
            return success(
                operation,
                {"truncated": True, "original_bytes": len(raw), "raw_ref": raw_ref},
                artifacts=[{"ref": raw_ref, "role": "raw_response"}],
                warnings=["Full response was stored because it exceeded the output budget."],
            )
        return {
            "ok": False,
            "operation": operation,
            "code": payload.get("code", "INTERNAL_ERROR"),
            "message": "Error details exceeded the output budget.",
            "target": None,
            "details": {"truncated": True, "original_bytes": len(raw)},
            "suggested_action": "Retrieve raw_ref for the complete error response.",
            "raw_ref": raw_ref,
        }
    return compact


def execute(
    operation: str,
    function: Callable[[], dict[str, Any]],
    *,
    pretty: bool = False,
    human: bool = False,
    debug: bool = False,
    full: bool = False,
) -> int:
    started = time.monotonic()
    try:
        payload = function()
        if "ok" not in payload:
            payload = success(operation, payload)
        if payload.get("ok") and "metrics" in payload:
            payload["metrics"]["duration_ms"] = round((time.monotonic() - started) * 1000)
        payload = _bounded_payload(payload, pretty=pretty, human=human, full=full)
        emit(payload, pretty=pretty, human=human)
        if payload.get("ok"):
            return 0
        return ERROR_EXIT_CODES.get(str(payload.get("code", "INTERNAL_ERROR")), 70)
    except AerError as error:
        if error.operation == "unknown":
            error.operation = operation
        payload = _bounded_payload(failure(error), pretty=pretty, human=human, full=full)
        emit(payload, pretty=pretty, human=human)
        if debug:
            traceback.print_exc(file=sys.stderr)
        return error.exit_code
    except Exception as exc:  # deliberate protocol boundary
        internal_error = AerError(
            "INTERNAL_ERROR", str(exc) or type(exc).__name__, operation=operation
        )
        payload = _bounded_payload(failure(internal_error), pretty=pretty, human=human, full=full)
        emit(payload, pretty=pretty, human=human)
        if debug:
            traceback.print_exc(file=sys.stderr)
        return internal_error.exit_code
