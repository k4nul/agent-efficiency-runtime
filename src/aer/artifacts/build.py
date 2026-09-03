from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from html import escape
from pathlib import Path
from typing import Any

from aer.artifacts.chart import build_chart
from aer.artifacts.common.spec import load_spec, manifest_path, write_manifest
from aer.artifacts.document import build_document
from aer.artifacts.presentation import build_presentation
from aer.artifacts.workbook import build_workbook
from aer.errors import AerError
from aer.hashing import normalized_hash
from aer.limits import MAX_PRESENTATION_ELEMENTS
from aer.paths import atomic_write_text, prepare_output_path


def _extension(kind: str) -> set[str]:
    return {
        "presentation": {".pptx"},
        "document": {".docx"},
        "workbook": {".xlsx"},
        "chart": {".png", ".svg"},
        "html": {".html", ".htm"},
        "markdown": {".md", ".markdown"},
    }[kind]


def _build_markup(
    spec: dict[str, Any], output: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kind = str(spec["kind"])
    content = spec.get("content", "")
    if isinstance(content, list):
        if kind == "markdown":
            chunks = []
            for item in content:
                if isinstance(item, dict):
                    block_type = item.get("type", "paragraph")
                    text = str(item.get("text", item.get("title", "")))
                    if block_type == "heading":
                        text = "#" * int(item.get("level", 1)) + " " + text
                    chunks.append(text)
                else:
                    chunks.append(str(item))
            rendered = "\n\n".join(chunks) + "\n"
        else:
            body = "\n".join(
                f"<p>{escape(str(item.get('text', '')))}</p>"
                if isinstance(item, dict)
                else f"<p>{escape(str(item))}</p>"
                for item in content
            )
            title = escape(str(spec.get("metadata", {}).get("title", "")))
            rendered = f'<!doctype html>\n<html lang="en"><meta charset="utf-8"><title>{title}</title><body>{body}</body></html>\n'
    else:
        if kind == "html":
            title = escape(str(spec.get("metadata", {}).get("title", "")))
            rendered = (
                '<!doctype html>\n<html lang="en"><meta charset="utf-8">'
                f"<title>{title}</title><body><p>{escape(str(content))}</p></body></html>\n"
            )
        else:
            rendered = str(content)
    atomic_write_text(output, rendered)
    return [{"id": "content", "type": kind, "selector": "/content"}], []


def plan_build(spec: dict[str, Any], output: Path | None = None) -> dict[str, Any]:
    kind = str(spec["kind"])
    return {
        "kind": kind,
        "version": spec["version"],
        "spec_sha256": normalized_hash(spec),
        "output": str(output) if output else None,
        "content_items": len(spec.get("content", spec.get("sheets", [])))
        if isinstance(spec.get("content", spec.get("sheets", [])), list)
        else 1,
        "writes": [] if output is None else [str(output), str(manifest_path(output))],
    }


def _enforce_presentation_materialization_limit(spec: dict[str, Any]) -> None:
    content = spec.get("content")
    if not isinstance(content, list):
        return
    materialized_items = 0
    pending: list[Any] = [content]
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            materialized_items += len(value)
            if materialized_items > MAX_PRESENTATION_ELEMENTS:
                raise AerError(
                    "LIMIT_EXCEEDED",
                    "Presentation exceeds the aggregate element limit.",
                    "presentation.build",
                    "/content",
                    {
                        "materialized_items": materialized_items,
                        "limit": MAX_PRESENTATION_ELEMENTS,
                    },
                )
            pending.extend(value)
        elif isinstance(value, dict):
            pending.extend(value.values())


def _run_builder(
    spec: dict[str, Any], output: Path, *, spec_dir: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kind = str(spec["kind"])
    builders = {
        "presentation": build_presentation,
        "document": build_document,
        "workbook": build_workbook,
        "chart": build_chart,
    }
    try:
        if kind in builders:
            return builders[kind](spec, output, spec_dir=spec_dir)
        return _build_markup(spec, output)
    except AerError:
        raise
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        missing = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else None
        raise AerError(
            "INVALID_SPEC",
            f"Artifact spec has an invalid nested value: {exc}",
            f"{kind}.build",
            f"/{missing}" if missing else "/content",
            suggested_action="Check the compact capability schema and required block fields.",
        ) from exc


def _validate_output_extension(kind: str, output: Path) -> None:
    if output.suffix.lower() not in _extension(kind):
        raise AerError(
            "INVALID_ARGUMENT",
            f"Output extension does not match {kind}.",
            f"{kind}.build",
            str(output),
            {"allowed_extensions": sorted(_extension(kind))},
        )


def _publish_build(
    staged_output: Path,
    staged_manifest: Path,
    output: Path,
    manifest: Path,
) -> None:
    backup_output = staged_output.with_name("previous-artifact")
    backup_manifest = staged_output.with_name("previous-manifest")
    moved_output = False
    moved_manifest = False
    published_output = False
    published_manifest = False
    try:
        if output.exists():
            os.replace(output, backup_output)
            moved_output = True
        if manifest.exists():
            os.replace(manifest, backup_manifest)
            moved_manifest = True
        os.replace(staged_output, output)
        published_output = True
        os.replace(staged_manifest, manifest)
        published_manifest = True
    except BaseException:
        if published_output:
            with suppress(FileNotFoundError):
                output.unlink()
        if published_manifest:
            with suppress(FileNotFoundError):
                manifest.unlink()
        if moved_output:
            os.replace(backup_output, output)
        if moved_manifest:
            os.replace(backup_manifest, manifest)
        raise


def build_artifact(
    spec_path: Path,
    output: Path | None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    spec = load_spec(spec_path)
    kind = str(spec["kind"])
    if kind == "presentation":
        _enforce_presentation_materialization_limit(spec)
    if dry_run:
        if output is not None:
            _validate_output_extension(kind, output)
        suffix = output.suffix if output is not None else sorted(_extension(kind))[0]
        with tempfile.TemporaryDirectory(prefix="aer-build-plan-") as directory:
            _run_builder(
                spec,
                Path(directory) / f"validation{suffix}",
                spec_dir=spec_path.resolve().parent,
            )
        return {"operation": f"{kind}.build", "plan": plan_build(spec, output), "dry_run": True}
    if output is None:
        raise AerError(
            "INVALID_ARGUMENT",
            "Output is required unless --dry-run is used.",
            "artifact.build",
            "output",
        )
    output = prepare_output_path(output, operation=f"{kind}.build")
    _validate_output_extension(kind, output)
    if output.exists() and not output.is_file():
        raise AerError(
            "INVALID_ARGUMENT",
            "Artifact output must be a file path.",
            f"{kind}.build",
            str(output),
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = prepare_output_path(manifest_path(output), operation=f"{kind}.build")
    if manifest.exists() and not manifest.is_file():
        raise AerError(
            "INVALID_ARGUMENT",
            "Artifact manifest output must be a file path.",
            f"{kind}.build",
            str(manifest),
        )
    with tempfile.TemporaryDirectory(prefix=".aer-build-", dir=output.parent) as directory:
        staged_output = Path(directory) / output.name
        elements, warnings = _run_builder(spec, staged_output, spec_dir=spec_path.resolve().parent)
        staged_manifest = write_manifest(staged_output, kind=kind, spec=spec, elements=elements)
        artifact_sha256 = json.loads(staged_manifest.read_text(encoding="utf-8"))["artifact_sha256"]
        _publish_build(staged_output, staged_manifest, output, manifest)
    return {
        "operation": f"{kind}.build",
        "output": str(output),
        "manifest": str(manifest),
        "sha256": artifact_sha256,
        "warnings": warnings,
        "element_count": len(elements),
    }
