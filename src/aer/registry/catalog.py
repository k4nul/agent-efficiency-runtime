"""Built-in capability catalog.

The catalog intentionally describes only executable runtime families. Discovery returns
one-line records; detailed schemas are disclosed only for the selected capability.
"""

from __future__ import annotations

from aer.registry.models import EMPTY_OUTPUT, Capability, object_schema


def _path(description: str) -> dict[str, str]:
    return {"type": "string", "format": "path", "description": description}


def _string(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


def _integer(description: str, default: int | None = None) -> dict[str, object]:
    value: dict[str, object] = {"type": "integer", "description": description}
    if default is not None:
        value["default"] = default
    return value


PATCH_COMMON = {
    "target": _path("File to patch."),
    "spec": _path("Versioned YAML or JSON patch specification."),
    "expected_sha256": _string("Optional stale-file precondition."),
    "dry_run": {"type": "boolean", "default": False},
    "backup": {"type": "boolean", "default": False},
    "validate": {"type": "boolean", "default": False},
}

PRESENTATION_GUIDANCE = {
    "spec_required": ["version=1", "kind=presentation", "content[]"],
    "slide": "Each slide needs id, layout, and layout fields.",
    "layouts": {
        "title|section|closing": "title, subtitle?",
        "bullets": "title?, items[]",
        "two-column|comparison": "title?, left{title,items}, right{title,items}",
        "metrics": "title?, metrics[{id,value,label}]",
        "table": "title?, headers[], rows[][]",
        "image|image-with-caption": "title?, source, caption?",
        "chart": "title?, chart_type, categories[], series[{name,values[]}]",
        "quote": "title?, quote, attribution?",
        "timeline": "title?, items[{id,label}]",
    },
}
DOCUMENT_GUIDANCE = {
    "spec_required": ["version=1", "kind=document", "content[]"],
    "block": "Each block should have a stable id and type.",
    "types": {
        "title|paragraph|caption|quote|callout": "text",
        "heading": "text, level?",
        "bullets|numbered-list|source-list": "items[]",
        "table": "headers[], rows[][]",
        "image": "source, width?",
        "page-break|section-break": "no additional fields",
    },
}
WORKBOOK_GUIDANCE = {
    "spec_required": ["version=1", "kind=workbook", "sheets[]"],
    "sheet": "id, name?, columns[]?, rows[]?, cells[]?",
    "cell": "id?, address, value? or formula?, number_format?",
    "optional": [
        "freeze",
        "auto_filter",
        "table",
        "column_widths",
        "conditional_formats",
        "charts",
        "named_range",
    ],
}
PRESENTATION_PATCH_GUIDANCE = {
    "patch_required": ["version=1", "operations[]"],
    "operation_fields": {
        "pptx.set_text": "target, value",
        "pptx.replace_text": "target?; find, replace",
        "pptx.remove_shape": "target",
        "pptx.update_chart_data": "target, categories[], series[{name,values[]}]",
    },
    "selector": "slide:id=<slide>/shape:id=<shape>",
}
DOCUMENT_PATCH_GUIDANCE = {
    "patch_required": ["version=1", "operations[]"],
    "operation_fields": {
        "docx.replace_text": "find, replace",
        "docx.set_block": "target=block:id=<id>, value",
        "docx.remove_block": "target=block:id=<id>",
    },
}
WORKBOOK_PATCH_GUIDANCE = {
    "patch_required": ["version=1", "operations[]"],
    "operation_fields": {
        "xlsx.set_cell": "target=Sheet!A1 or stable cell selector, value|formula",
        "xlsx.set_range": "target=Sheet!A1:B2, values[][]",
        "xlsx.replace_text": "target=Sheet!A1:B20, find, replace",
        "xlsx.clear_range": "target=Sheet!A1:B20",
    },
}


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "store.content",
        "Put, retrieve, inspect, verify, pin, or collect content-addressed objects.",
        ("store", "sha256", "object", "raw", "ref", "deduplicate", "gc"),
        object_schema(
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "put",
                        "get",
                        "cat",
                        "stat",
                        "verify",
                        "list",
                        "gc",
                        "pin",
                        "unpin",
                    ],
                },
                "target": _string("Local path or aer://sha256 reference."),
                "output": _path("Optional output file."),
                "stdin": {"type": "boolean", "default": False},
                "filename": _string("Original filename metadata for stdin content."),
                "mime_type": _string("Explicit MIME type metadata."),
                "pin": {"type": "boolean", "default": False},
                "start_line": _integer("First one-based line for cat."),
                "end_line": _integer("Last one-based line for cat."),
                "limit": _integer("Maximum listed objects.", 20),
                "offset": _integer("List offset.", 0),
                "older_than": _string("GC age such as 30d."),
                "dry_run": {"type": "boolean", "default": False},
                "overwrite": {"type": "boolean", "default": False},
            },
            ("action",),
        ),
        EMPTY_OUTPUT,
        ({"action": "put", "target": "report.docx"},),
        operations=("put", "get", "cat", "stat", "verify", "list", "gc", "pin", "unpin"),
    ),
    Capability(
        "artifact.inspect",
        "Inspect a selected, bounded part of a file or repository.",
        (
            "inspect",
            "summary",
            "outline",
            "selector",
            "query",
            "json",
            "yaml",
            "csv",
            "xlsx",
            "pptx",
            "docx",
            "pdf",
            "repository",
        ),
        object_schema(
            {
                "target": _string("Local path or aer://sha256 reference."),
                "summary": {"type": "boolean", "default": True},
                "outline": {"type": "boolean", "default": False},
                "selector": _string("Format-specific selector."),
                "query": _string("Bounded literal or regular-expression search."),
                "regex": {"type": "boolean", "default": False},
                "case_sensitive": {"type": "boolean", "default": False},
                "context": _integer("Context lines around matches.", 0),
                "max_items": _integer("Maximum preview items.", 20),
                "start_line": _integer("First one-based text line."),
                "end_line": _integer("Last one-based text line."),
                "sheet": _string("XLSX sheet name."),
                "range": _string("XLSX cell range such as A1:F20."),
                "formulas": {"type": "boolean", "default": False},
                "rows": _string("XLSX row range such as 100:120."),
                "slide": _integer("One-based PPTX slide number."),
                "page": _integer("One-based PDF page number."),
                "glob": _string("Repository file glob."),
                "changed": {"type": "boolean", "default": False},
                "max_depth": _integer("JSON/YAML outline depth.", 6),
            },
            ("target",),
        ),
        EMPTY_OUTPUT,
        ({"target": "result.json", "selector": "/items/3/status"},),
    ),
    Capability(
        "command.run",
        "Run argv without a shell and return a compact, redacted log summary.",
        ("run", "command", "test", "build", "log", "pytest", "npm", "timeout"),
        object_schema(
            {
                "argv": {"type": "array", "items": {"type": "string"}},
                "cwd": _path("Working directory."),
                "timeout": _integer("Timeout in seconds.", 300),
            },
            ("argv",),
        ),
        EMPTY_OUTPUT,
        ({"argv": ["pytest", "-q"], "timeout": 300},),
        risk="medium",
    ),
    Capability(
        "data.query",
        "Filter, project, sort, and aggregate tabular data locally.",
        (
            "data",
            "csv",
            "tsv",
            "jsonl",
            "xlsx",
            "filter",
            "rows",
            "group",
            "sum",
            "average",
            "unique",
        ),
        object_schema(
            {
                "source": _path("CSV, TSV, JSON array, JSONL, or XLSX source."),
                "sheet": _string("XLSX sheet name."),
                "where": {"type": "array", "items": {"type": "string"}},
                "select": {"type": "array", "items": {"type": "string"}},
                "rename": {"type": "array", "items": {"type": "string"}},
                "sort": _string("Column used for sorting."),
                "descending": {"type": "boolean", "default": False},
                "limit": _integer("Maximum result rows."),
                "offset": _integer("Rows skipped after filtering.", 0),
                "unique": {"type": "boolean", "default": False},
                "unique_columns": _string("Comma-separated unique-key columns."),
                "group_by": _string("Comma-separated grouping columns."),
                "aggregate": {"type": "array", "items": {"type": "string"}},
                "duplicates": _string("Comma-separated duplicate-key columns."),
                "output": _path("Optional result file."),
            },
            ("source",),
        ),
        EMPTY_OUTPUT,
        (
            {
                "source": "orders.xlsx",
                "sheet": "Raw",
                "where": ["status == pending"],
                "select": ["id", "total"],
            },
        ),
        operations=(
            "select",
            "rename",
            "filter",
            "sort",
            "limit",
            "offset",
            "unique",
            "count",
            "sum",
            "average",
            "min",
            "max",
            "null_count",
            "duplicates",
        ),
    ),
    Capability(
        "presentation.build",
        "Build a PPTX presentation from a versioned semantic specification.",
        ("ppt", "pptx", "slides", "deck", "presentation", "build", "create"),
        object_schema(
            {
                "spec": _path("Presentation YAML or JSON specification."),
                "output": _path("Destination PPTX; required unless dry_run is true."),
                "validate": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": False},
            },
            ("spec",),
        ),
        EMPTY_OUTPUT,
        (
            {
                "command": {"spec": "presentation.yaml", "output": "deck.pptx"},
                "spec": {
                    "version": 1,
                    "kind": "presentation",
                    "content": [{"id": "cover", "layout": "title", "title": "AER"}],
                },
            },
        ),
        guidance=PRESENTATION_GUIDANCE,
    ),
    Capability(
        "document.build",
        "Build a DOCX document from a versioned semantic specification.",
        ("doc", "docx", "word", "report", "document", "build", "create"),
        object_schema(
            {
                "spec": _path("Document specification."),
                "output": _path("Destination DOCX; required unless dry_run is true."),
                "validate": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": False},
            },
            ("spec",),
        ),
        EMPTY_OUTPUT,
        (
            {
                "command": {"spec": "document.yaml", "output": "report.docx"},
                "spec": {
                    "version": 1,
                    "kind": "document",
                    "content": [{"id": "summary", "type": "paragraph", "text": "Result"}],
                },
            },
        ),
        guidance=DOCUMENT_GUIDANCE,
    ),
    Capability(
        "workbook.build",
        "Build an XLSX workbook from a versioned semantic specification.",
        ("excel", "xlsx", "spreadsheet", "workbook", "formula", "build", "create"),
        object_schema(
            {
                "spec": _path("Workbook specification."),
                "output": _path("Destination XLSX; required unless dry_run is true."),
                "validate": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": False},
            },
            ("spec",),
        ),
        EMPTY_OUTPUT,
        (
            {
                "command": {"spec": "workbook.yaml", "output": "metrics.xlsx"},
                "spec": {
                    "version": 1,
                    "kind": "workbook",
                    "sheets": [{"id": "data", "columns": ["id"], "rows": [[1]]}],
                },
            },
        ),
        guidance=WORKBOOK_GUIDANCE,
    ),
    Capability(
        "chart.build",
        "Render a PNG or SVG chart from data and a semantic chart specification.",
        ("chart", "graph", "plot", "png", "svg", "bar", "line", "pie", "scatter"),
        object_schema(
            {
                "spec": _path("Chart specification."),
                "output": _path("Destination PNG or SVG; required unless dry_run is true."),
                "validate": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": False},
            },
            ("spec",),
        ),
        EMPTY_OUTPUT,
        (
            {
                "command": {"spec": "chart.yaml", "output": "chart.png"},
                "spec": {
                    "version": 1,
                    "kind": "chart",
                    "type": "bar",
                    "source": "data.csv",
                    "x": "workflow",
                    "y": "tokens",
                },
                "data_csv": "workflow,tokens\ndirect,100\naer,40\n",
            },
        ),
        operations=("bar", "horizontal-bar", "line", "area", "pie", "scatter"),
    ),
    Capability(
        "markup.build",
        "Build deterministic HTML or Markdown from a semantic specification.",
        ("html", "markdown", "md", "markup", "build"),
        object_schema(
            {
                "spec": _path("Markup specification."),
                "output": _path("Destination file; required unless dry_run is true."),
                "validate": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": False},
            },
            ("spec",),
        ),
        EMPTY_OUTPUT,
        (
            {
                "command": {"spec": "page.yaml", "output": "page.html"},
                "spec": {
                    "version": 1,
                    "kind": "html",
                    "metadata": {"title": "AER"},
                    "content": [{"type": "paragraph", "text": "Deterministic output"}],
                },
            },
        ),
        guidance={
            "spec_required": ["version=1", "kind=html|markdown", "content"],
            "content": "UTF-8 string or a small semantic block array.",
        },
    ),
    Capability(
        "text.patch",
        "Apply bounded literal or regular-expression replacements to text.",
        ("text", "replace", "regex", "edit", "patch"),
        object_schema(PATCH_COMMON, ("target", "spec")),
        EMPTY_OUTPUT,
        operations=("text.replace", "text.regex_replace"),
    ),
    Capability(
        "json.patch",
        "Patch selected JSON values with JSON Pointer targets.",
        ("json", "pointer", "set", "remove", "insert", "edit", "patch"),
        object_schema(PATCH_COMMON, ("target", "spec")),
        EMPTY_OUTPUT,
        operations=("json.set", "json.remove", "json.insert"),
    ),
    Capability(
        "yaml.patch",
        "Patch selected YAML values using safely loaded paths.",
        ("yaml", "yml", "set", "remove", "insert", "edit", "patch"),
        object_schema(PATCH_COMMON, ("target", "spec")),
        EMPTY_OUTPUT,
        operations=("yaml.set", "yaml.remove", "yaml.insert"),
    ),
    Capability(
        "presentation.patch",
        "Patch selected presentation elements by stable ID.",
        ("ppt", "pptx", "slide", "title", "shape", "replace", "edit", "patch"),
        object_schema(PATCH_COMMON, ("target", "spec")),
        EMPTY_OUTPUT,
        (
            {
                "command": {"target": "deck.pptx", "spec": "patch.yaml"},
                "patch": {
                    "version": 1,
                    "operations": [
                        {
                            "op": "pptx.set_text",
                            "target": "slide:id=metrics/shape:id=value",
                            "value": "63%",
                        }
                    ],
                },
            },
        ),
        operations=(
            "pptx.set_text",
            "pptx.replace_text",
            "pptx.remove_shape",
            "pptx.update_chart_data",
        ),
        guidance=PRESENTATION_PATCH_GUIDANCE,
    ),
    Capability(
        "document.patch",
        "Patch selected DOCX blocks or text while preserving other content.",
        ("doc", "docx", "word", "block", "text", "replace", "edit", "patch"),
        object_schema(PATCH_COMMON, ("target", "spec")),
        EMPTY_OUTPUT,
        (
            {
                "command": {"target": "report.docx", "spec": "patch.yaml"},
                "patch": {
                    "version": 1,
                    "operations": [
                        {"op": "docx.set_block", "target": "block:id=summary", "value": "Done"}
                    ],
                },
            },
        ),
        operations=("docx.replace_text", "docx.set_block", "docx.remove_block"),
        guidance=DOCUMENT_PATCH_GUIDANCE,
    ),
    Capability(
        "workbook.patch",
        "Patch selected XLSX cells or ranges while preserving formulas and sheets.",
        ("excel", "xlsx", "workbook", "cell", "range", "formula", "replace", "edit", "patch"),
        object_schema(PATCH_COMMON, ("target", "spec")),
        EMPTY_OUTPUT,
        (
            {
                "command": {"target": "metrics.xlsx", "spec": "patch.yaml"},
                "patch": {
                    "version": 1,
                    "operations": [{"op": "xlsx.set_cell", "target": "Summary!D2", "value": 63}],
                },
            },
        ),
        operations=("xlsx.set_cell", "xlsx.set_range", "xlsx.replace_text", "xlsx.clear_range"),
        guidance=WORKBOOK_PATCH_GUIDANCE,
    ),
    Capability(
        "artifact.validate",
        "Validate structural integrity and bounded quality checks for an artifact.",
        ("validate", "verify", "pptx", "docx", "xlsx", "pdf", "render", "strict"),
        object_schema(
            {
                "target": _path("Artifact to validate."),
                "render": {"type": "boolean", "default": False},
                "strict": {"type": "boolean", "default": False},
            },
            ("target",),
        ),
        EMPTY_OUTPUT,
        requires=("LibreOffice for Office --render; pdftoppm for PDF --render",),
        guidance={
            "render": "Machine conversion/raster evidence only; human visual review remains required.",
            "dependencies": "Office --render uses LibreOffice; PDF --render uses pdftoppm.",
        },
    ),
    Capability(
        "artifact.convert",
        "Convert supported local artifact formats with explicit dependency checks.",
        ("convert", "office", "pdf", "csv", "xlsx", "image", "webp"),
        object_schema(
            {"source": _path("Source file."), "output": _path("Destination file.")},
            ("source", "output"),
        ),
        EMPTY_OUTPUT,
        requires=("format-dependent",),
    ),
    Capability(
        "image.transform",
        "Inspect, resize, crop, fit, or batch-process raster images deterministically.",
        ("image", "png", "jpeg", "webp", "resize", "crop", "contain", "cover", "batch"),
        object_schema(
            {
                "action": {"type": "string", "enum": ["inspect", "resize", "crop", "fit", "batch"]},
                "source": _string("Image path or safe local glob."),
                "output": _path("Destination file or directory."),
                "width": _integer("Output width in pixels."),
                "height": _integer("Output height in pixels."),
                "x": _integer("Crop left coordinate."),
                "y": _integer("Crop top coordinate."),
                "ratio": _string("Fit ratio such as 4:5."),
                "mode": {"type": "string", "enum": ["contain", "cover"], "default": "cover"},
                "background": _string("Contain-mode canvas color."),
                "out_dir": _path("Batch output directory."),
                "strip_metadata": {"type": "boolean", "default": False},
                "overwrite": {"type": "boolean", "default": False},
            },
            ("action", "source"),
        ),
        EMPTY_OUTPUT,
        operations=("inspect", "resize", "crop", "fit", "batch"),
    ),
    Capability(
        "pdf.inspect",
        "Inspect PDF metadata and selected pages without returning the whole document.",
        ("pdf", "inspect", "pages", "metadata", "text", "encrypted"),
        object_schema(
            {"target": _path("PDF file."), "page": _integer("One-based page number.")}, ("target",)
        ),
        EMPTY_OUTPUT,
    ),
    Capability(
        "pdf.merge",
        "Merge local PDF files into one atomic output.",
        ("pdf", "merge", "combine", "join"),
        object_schema(
            {
                "inputs": {"type": "array", "items": {"type": "string"}},
                "output": _path("Merged PDF."),
            },
            ("inputs", "output"),
        ),
        EMPTY_OUTPUT,
    ),
    Capability(
        "pdf.extract",
        "Extract selected PDF pages into a new document.",
        ("pdf", "extract", "pages", "excerpt"),
        object_schema(
            {
                "source": _path("Source PDF."),
                "pages": _string("One-based page selector."),
                "output": _path("Extracted PDF."),
            },
            ("source", "pages", "output"),
        ),
        EMPTY_OUTPUT,
    ),
    Capability(
        "pdf.split",
        "Split a PDF into deterministic per-page files.",
        ("pdf", "split", "pages"),
        object_schema(
            {"source": _path("Source PDF."), "output_dir": _path("Destination directory.")},
            ("source", "output_dir"),
        ),
        EMPTY_OUTPUT,
    ),
    Capability(
        "archive.create",
        "Create a deterministic ZIP with a hash manifest.",
        ("archive", "zip", "package", "deterministic", "manifest", "create"),
        object_schema(
            {
                "source": _path("Source directory."),
                "output": _path("Destination ZIP."),
                "exclude": {"type": "array", "items": {"type": "string"}},
                "dry_run": {"type": "boolean", "default": False},
            },
            ("source", "output"),
        ),
        EMPTY_OUTPUT,
    ),
    Capability(
        "archive.verify",
        "List or verify ZIP entries, hashes, and safe paths.",
        ("archive", "zip", "list", "verify", "manifest"),
        object_schema(
            {
                "target": _path("Archive path."),
                "action": {"type": "string", "enum": ["list", "verify"]},
            },
            ("target", "action"),
        ),
        EMPTY_OUTPUT,
    ),
    Capability(
        "task.state",
        "Persist compact task progress, decisions, artifacts, and checkpoints.",
        ("state", "task", "checkpoint", "decision", "remaining", "completed"),
        object_schema(
            {
                "action": {
                    "type": "string",
                    "enum": ["init", "update", "show", "list", "checkpoint", "export"],
                },
                "task_id": _string("Stable task identifier."),
                "goal": _string("Task goal used by init."),
                "complete": {"type": "array", "items": {"type": "string"}},
                "remaining": {"type": "array", "items": {"type": "string"}},
                "decision": {"type": "array", "items": {"type": "string"}},
                "artifact": {"type": "array", "items": {"type": "string"}},
                "warning": {"type": "array", "items": {"type": "string"}},
                "status": _string("Task status."),
                "output": _path("Optional export path."),
                "limit": _integer("Maximum tasks returned by list.", 20),
            },
            ("action",),
        ),
        EMPTY_OUTPUT,
    ),
    Capability(
        "recipe.run",
        "Validate or run a trusted, capability-only workflow recipe.",
        ("recipe", "workflow", "steps", "cache", "trusted", "run", "validate"),
        object_schema(
            {
                "recipe": _string("Built-in recipe name or trusted recipe path."),
                "variables": {"type": "object"},
                "dry_run": {"type": "boolean", "default": False},
                "trust": {"type": "boolean", "default": False},
                "allow_raw_command": {"type": "boolean", "default": False},
                "timeout": _integer("Overall recipe timeout in seconds.", 600),
            },
            ("recipe",),
        ),
        EMPTY_OUTPUT,
        risk="medium",
    ),
    Capability(
        "profile.record",
        "Record and compare caller-supplied agent calls, tokens, retries, duration, and success.",
        ("profile", "tokens", "calls", "retries", "success", "compare", "metrics"),
        object_schema(
            {
                "task": _string("Task family."),
                "variant": _string("Compared workflow variant."),
                "model": _string("Caller-supplied provider model identifier."),
                "model_calls": _integer("Model call count."),
                "tool_calls": _integer("Tool call count."),
                "input_tokens": _integer("Caller-supplied input tokens."),
                "cached_input_tokens": _integer("Caller-supplied cached input tokens."),
                "output_tokens": _integer("Caller-supplied output tokens."),
                "reasoning_tokens": _integer("Caller-supplied reasoning tokens."),
                "tool_schema_tokens": _integer("Caller-supplied tool schema tokens."),
                "tool_result_tokens": _integer("Caller-supplied tool result tokens."),
                "retries": _integer("Retry count."),
                "duration_ms": _integer("Caller-supplied duration in milliseconds."),
                "success": {"type": "boolean"},
                "human_edits": _integer("Human edit count."),
                "notes": _string("Provenance or measurement notes."),
            },
            ("task", "variant", "success"),
        ),
        EMPTY_OUTPUT,
    ),
    Capability(
        "benchmark.run",
        "Measure direct versus compact workflows using generated local fixtures.",
        ("benchmark", "token", "bytes", "log", "data", "patch", "recipe"),
        object_schema({"scenario": _string("Optional built-in scenario name.")}),
        EMPTY_OUTPUT,
    ),
    Capability(
        "runtime.doctor",
        "Check runtime storage, libraries, tools, templates, and recipes.",
        ("doctor", "diagnose", "dependencies", "libreoffice", "pandoc", "git", "ripgrep"),
        object_schema({}),
        EMPTY_OUTPUT,
    ),
)
