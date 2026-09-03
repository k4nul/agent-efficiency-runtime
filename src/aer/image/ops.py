from __future__ import annotations

import glob
import io
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from aer.errors import AerError
from aer.hashing import sha256_file
from aer.limits import MAX_IMAGE_PIXELS
from aer.paths import (
    atomic_write_bytes,
    atomic_write_text,
    ensure_regular_input,
    prepare_output_path,
)

FORMAT_BY_SUFFIX = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
    ".tif": "TIFF",
    ".tiff": "TIFF",
}


def _open(path: Path) -> Image.Image:
    source = ensure_regular_input(path, operation="image.process")
    try:
        image = Image.open(source)
        if image.width * image.height > MAX_IMAGE_PIXELS:
            image.close()
            raise AerError(
                "LIMIT_EXCEEDED",
                "Image pixel count exceeds the safety limit.",
                "image.process",
                str(source),
            )
        image.load()
        format_name = image.format
        oriented = ImageOps.exif_transpose(image)
        if oriented is not image:
            image.close()
        oriented.format = format_name
        return oriented
    except AerError:
        raise
    except Exception as exc:
        raise AerError(
            "CORRUPT_FILE", f"Cannot open image: {exc}", "image.process", str(source)
        ) from exc


def _save(
    image: Image.Image, output: Path, *, strip_metadata: bool, overwrite: bool
) -> dict[str, Any]:
    output = prepare_output_path(output, operation="image.process")
    if output.exists() and not overwrite:
        raise AerError(
            "CONFLICT",
            "Output already exists; use --overwrite to replace it.",
            "image.process",
            str(output),
        )
    format_name = FORMAT_BY_SUFFIX.get(output.suffix.lower())
    if not format_name:
        raise AerError(
            "UNSUPPORTED_FORMAT",
            "Image output must be PNG, JPEG, WEBP, or TIFF.",
            "image.process",
            str(output),
        )
    converted = image
    if format_name == "JPEG" and image.mode not in {"RGB", "L"}:
        background = Image.new("RGB", image.size, "white")
        if image.mode == "RGBA":
            background.paste(image, mask=image.getchannel("A"))
        else:
            background.paste(image.convert("RGB"))
        converted = background
    options: dict[str, Any] = {"format": format_name, "optimize": True}
    if not strip_metadata and image.info.get("exif"):
        options["exif"] = image.info["exif"]
    buffer = io.BytesIO()
    converted.save(buffer, **options)
    atomic_write_bytes(output, buffer.getvalue())
    return {
        "output": str(output),
        "width": converted.width,
        "height": converted.height,
        "format": format_name,
        "sha256": sha256_file(output),
    }


def inspect_image(path: Path) -> dict[str, Any]:
    source = ensure_regular_input(path, operation="image.inspect")
    with _open(source) as image:
        return {
            "path": str(source),
            "format": image.format,
            "width": image.width,
            "height": image.height,
            "mode": image.mode,
            "aspect_ratio": image.width / image.height,
            "has_exif": bool(image.getexif()),
            "bytes": source.stat().st_size,
        }


def resize_image(
    path: Path,
    output: Path,
    *,
    width: int | None = None,
    height: int | None = None,
    strip_metadata: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    if width is None and height is None:
        raise AerError(
            "INVALID_ARGUMENT", "At least one of width or height is required.", "image.resize"
        )
    if (width is not None and width <= 0) or (height is not None and height <= 0):
        raise AerError("INVALID_ARGUMENT", "Image dimensions must be positive.", "image.resize")
    with _open(path) as image:
        if width is None:
            assert height is not None
            width = round(image.width * height / image.height)
        if height is None:
            height = round(image.height * width / image.width)
        if width * height > MAX_IMAGE_PIXELS:
            raise AerError(
                "LIMIT_EXCEEDED",
                "Requested image dimensions exceed the pixel safety limit.",
                "image.resize",
                details={"width": width, "height": height, "limit": MAX_IMAGE_PIXELS},
            )
        resized = image.resize((width, height), Image.Resampling.LANCZOS)
        return _save(resized, output, strip_metadata=strip_metadata, overwrite=overwrite)


def crop_image(
    path: Path,
    output: Path,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    strip_metadata: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    if min(x, y) < 0 or width <= 0 or height <= 0:
        raise AerError(
            "INVALID_ARGUMENT", "Crop coordinates and dimensions are invalid.", "image.crop"
        )
    with _open(path) as image:
        if x + width > image.width or y + height > image.height:
            raise AerError(
                "INVALID_ARGUMENT", "Crop rectangle exceeds the image bounds.", "image.crop"
            )
        cropped = image.crop((x, y, x + width, y + height))
        return _save(cropped, output, strip_metadata=strip_metadata, overwrite=overwrite)


def _ratio(value: str) -> float:
    try:
        left, right = value.split(":", 1)
        ratio = float(left) / float(right)
    except (ValueError, ZeroDivisionError) as exc:
        raise AerError("INVALID_ARGUMENT", "Ratio must look like 4:5.", "image.fit", value) from exc
    if ratio <= 0:
        raise AerError("INVALID_ARGUMENT", "Ratio must be positive.", "image.fit", value)
    return ratio


def fit_image(
    path: Path,
    output: Path,
    *,
    ratio: str,
    mode: str = "cover",
    width: int | None = None,
    background: str = "white",
    strip_metadata: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    target_ratio = _ratio(ratio)
    if mode not in {"cover", "contain"}:
        raise AerError("INVALID_ARGUMENT", "Fit mode must be cover or contain.", "image.fit", mode)
    if width is not None and width <= 0:
        raise AerError("INVALID_ARGUMENT", "Fit width must be positive.", "image.fit", str(width))
    with _open(path) as image:
        target_width = width or image.width
        target_height = max(1, round(target_width / target_ratio))
        if target_width * target_height > MAX_IMAGE_PIXELS:
            raise AerError(
                "LIMIT_EXCEEDED",
                "Requested fit dimensions exceed the pixel safety limit.",
                "image.fit",
                details={
                    "width": target_width,
                    "height": target_height,
                    "limit": MAX_IMAGE_PIXELS,
                },
            )
        if mode == "cover":
            fitted = ImageOps.fit(image, (target_width, target_height), Image.Resampling.LANCZOS)
        else:
            fitted = ImageOps.contain(
                image, (target_width, target_height), Image.Resampling.LANCZOS
            )
            canvas = Image.new(
                "RGBA" if fitted.mode == "RGBA" else "RGB",
                (target_width, target_height),
                background,
            )
            canvas.paste(
                fitted,
                ((target_width - fitted.width) // 2, (target_height - fitted.height) // 2),
                fitted if fitted.mode == "RGBA" else None,
            )
            fitted = canvas
        return _save(fitted, output, strip_metadata=strip_metadata, overwrite=overwrite)


def batch_images(
    pattern: str,
    output_dir: Path,
    *,
    width: int,
    strip_metadata: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    matches = sorted(Path(value) for value in glob.glob(pattern))
    if not matches:
        raise AerError("NOT_FOUND", "Image batch pattern matched no files.", "image.batch", pattern)
    output_dir = prepare_output_path(output_dir, operation="image.batch")
    destinations = [output_dir / source.name for source in matches]
    if len(set(destinations)) != len(destinations):
        raise AerError(
            "CONFLICT",
            "Image batch inputs contain duplicate output filenames.",
            "image.batch",
            str(output_dir),
        )
    manifest = output_dir / "manifest.json"
    if not overwrite:
        conflicts = [path for path in [*destinations, manifest] if path.exists()]
        if conflicts:
            raise AerError(
                "CONFLICT",
                "Batch output already exists; use --overwrite to replace it.",
                "image.batch",
                str(conflicts[0]),
                {"conflicts": [str(path) for path in conflicts[:20]]},
            )
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for source, destination in zip(matches, destinations, strict=True):
        results.append(
            resize_image(
                source,
                destination,
                width=width,
                strip_metadata=strip_metadata,
                overwrite=overwrite,
            )
        )
    atomic_write_text(
        manifest, json.dumps({"version": 1, "files": results}, indent=2, sort_keys=True) + "\n"
    )
    return {
        "output_dir": str(output_dir),
        "count": len(results),
        "manifest": str(manifest),
        "files": results[:20],
    }
