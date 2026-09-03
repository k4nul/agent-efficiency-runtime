from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt

from aer.errors import AerError
from aer.limits import MAX_IMAGE_PIXELS, MAX_TABULAR_FILE_BYTES, MAX_TABULAR_ROWS
from aer.paths import atomic_write_bytes, ensure_regular_input

plt.switch_backend("Agg")
plt.rcParams["font.family"] = ["NanumGothic", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def _rows(source: Path) -> list[dict[str, Any]]:
    source = ensure_regular_input(source, operation="chart.build")
    if source.stat().st_size > MAX_TABULAR_FILE_BYTES:
        raise AerError(
            "LIMIT_EXCEEDED",
            "Chart data source exceeds the size limit.",
            "chart.build",
            str(source),
            {"bytes": source.stat().st_size, "limit": MAX_TABULAR_FILE_BYTES},
        )
    if source.suffix.lower() in {".csv", ".tsv"}:
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(
                csv.DictReader(handle, delimiter="\t" if source.suffix.lower() == ".tsv" else ",")
            )
            if len(rows) > MAX_TABULAR_ROWS:
                raise AerError(
                    "LIMIT_EXCEEDED",
                    "Chart data contains too many rows.",
                    "chart.build",
                    str(source),
                    {"rows": len(rows), "limit": MAX_TABULAR_ROWS},
                )
            return rows
    if source.suffix.lower() == ".json":
        value = json.loads(source.read_text(encoding="utf-8"))
        if isinstance(value, list) and all(isinstance(row, dict) for row in value):
            if len(value) > MAX_TABULAR_ROWS:
                raise AerError(
                    "LIMIT_EXCEEDED",
                    "Chart data contains too many rows.",
                    "chart.build",
                    str(source),
                    {"rows": len(value), "limit": MAX_TABULAR_ROWS},
                )
            return value
    raise AerError(
        "UNSUPPORTED_FORMAT",
        "Chart source must be CSV, TSV, or a JSON array.",
        "chart.build",
        str(source),
    )


def build_chart(
    spec: dict[str, Any], output: Path, *, spec_dir: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_source = spec.get("source")
    if not raw_source:
        raise AerError("INVALID_SPEC", "Chart spec requires source.", "chart.build", "/source")
    source = (
        spec_dir / str(raw_source)
        if not Path(str(raw_source)).is_absolute()
        else Path(str(raw_source))
    )
    rows = _rows(source)
    x_name, y_name = str(spec.get("x", "")), str(spec.get("y", ""))
    if not x_name or not y_name:
        raise AerError("INVALID_SPEC", "Chart spec requires x and y fields.", "chart.build", "/x")
    try:
        x_values = [row[x_name] for row in rows]
        y_values = [float(row[y_name]) for row in rows]
    except (KeyError, TypeError, ValueError) as exc:
        raise AerError(
            "INVALID_SPEC", f"Invalid chart data: {exc}", "chart.build", str(source)
        ) from exc
    output_options = spec.get("output", {})
    width, height = int(output_options.get("width", 1400)), int(output_options.get("height", 900))
    dpi = int(output_options.get("dpi", 100))
    if width <= 0 or height <= 0 or dpi <= 0:
        raise AerError(
            "INVALID_SPEC", "Chart dimensions and DPI must be positive.", "chart.build", "/output"
        )
    if width * height > MAX_IMAGE_PIXELS:
        raise AerError(
            "LIMIT_EXCEEDED",
            "Chart dimensions exceed the pixel safety limit.",
            "chart.build",
            "/output",
            {"width": width, "height": height, "limit": MAX_IMAGE_PIXELS},
        )
    figure, axis = plt.subplots(figsize=(width / dpi, height / dpi), dpi=dpi)
    chart_type = str(spec.get("type", "bar"))
    color = str(spec.get("color", "#2769C1"))
    if chart_type == "bar":
        axis.bar(x_values, y_values, color=color)
    elif chart_type == "horizontal-bar":
        axis.barh(x_values, y_values, color=color)
    elif chart_type == "line":
        axis.plot(x_values, y_values, marker="o", color=color)
    elif chart_type == "area":
        axis.fill_between(range(len(x_values)), y_values, color=color, alpha=0.75)
        axis.set_xticks(range(len(x_values)), x_values)
    elif chart_type == "pie":
        axis.pie(y_values, labels=x_values, autopct="%1.1f%%")
    elif chart_type == "scatter":
        try:
            numeric_x = [float(value) for value in x_values]
        except ValueError as exc:
            raise AerError(
                "INVALID_SPEC",
                "Scatter x values must be numeric.",
                "chart.build",
                f"column:{x_name}",
            ) from exc
        axis.scatter(numeric_x, y_values, color=color)
    else:
        raise AerError(
            "INVALID_SPEC", f"Unsupported chart type: {chart_type}", "chart.build", "/type"
        )
    axis.set_title(str(spec.get("title", "")))
    if chart_type != "pie":
        axis.set_xlabel(str(spec.get("x_label", x_name)))
        axis.set_ylabel(str(spec.get("y_label", y_name)))
        axis.grid(axis="y", alpha=0.2)
    figure.tight_layout()
    format_name = output.suffix.lower().lstrip(".") or "png"
    if format_name not in {"png", "svg"}:
        plt.close(figure)
        raise AerError(
            "UNSUPPORTED_FORMAT", "Chart output must be PNG or SVG.", "chart.build", str(output)
        )
    buffer = io.BytesIO()
    metadata = (
        {"Software": "Agent Efficiency Runtime"}
        if format_name == "png"
        else {"Creator": "Agent Efficiency Runtime", "Date": None}
    )
    figure.savefig(
        buffer,
        format=format_name,
        dpi=dpi,
        metadata=metadata,
    )
    plt.close(figure)
    atomic_write_bytes(output, buffer.getvalue())
    return [{"id": "chart", "type": chart_type, "selector": "chart:id=chart"}], []
