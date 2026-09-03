from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aer.errors import AerError
from aer.hashing import normalized_hash, sha256_file
from aer.limits import MAX_SPEC_FILE_BYTES
from aer.paths import atomic_write_text, ensure_regular_input
from aer.yaml_safety import load_yaml_safely


def load_spec(path: Path) -> dict[str, Any]:
    source = ensure_regular_input(path, operation="artifact.build")
    if source.stat().st_size > MAX_SPEC_FILE_BYTES:
        raise AerError(
            "LIMIT_EXCEEDED",
            "Artifact spec exceeds the size limit.",
            "artifact.build",
            str(source),
            {"bytes": source.stat().st_size, "limit": MAX_SPEC_FILE_BYTES},
        )
    try:
        text = source.read_text(encoding="utf-8")
        value = (
            json.loads(text)
            if source.suffix.lower() == ".json"
            else load_yaml_safely(text, operation="artifact.build", target=str(path))
        )
    except AerError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AerError(
            "INVALID_SPEC",
            f"Cannot parse artifact spec: {exc}",
            "artifact.build",
            str(path),
        ) from exc
    if not isinstance(value, dict):
        raise AerError("INVALID_SPEC", "Spec root must be an object.", "artifact.build", "/")
    if value.get("version") != 1:
        raise AerError(
            "INVALID_SPEC",
            "Only artifact spec version 1 is supported.",
            "artifact.build",
            "/version",
            suggested_action="Set version: 1",
        )
    kind = value.get("kind")
    if kind not in {"presentation", "document", "workbook", "chart", "html", "markdown"}:
        raise AerError(
            "INVALID_SPEC",
            "Unsupported artifact kind.",
            "artifact.build",
            "/kind",
            {"supported": ["presentation", "document", "workbook", "chart", "html", "markdown"]},
        )
    return value


def manifest_path(output: Path) -> Path:
    return output.with_name(output.name + ".aer.json")


def write_manifest(
    output: Path, *, kind: str, spec: dict[str, Any], elements: list[dict[str, Any]]
) -> Path:
    path = manifest_path(output)
    payload = {
        "version": 1,
        "kind": kind,
        "artifact": output.name,
        "artifact_sha256": sha256_file(output),
        "spec_sha256": normalized_hash(spec),
        "elements": elements,
    }
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return path
