from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfWriter

import aer.archive.ops as archive_ops
import aer.conversion.ops as conversion_ops
import aer.pdf.ops as pdf_ops
from aer.archive import verify_archive
from aer.artifacts import build_artifact
from aer.config import Settings
from aer.conversion import convert_file
from aer.data import query_data
from aer.errors import AerError
from aer.patch import apply_patch
from aer.pdf import split_pdf
from aer.recipes import RecipeEngine
from aer.runner import run_command
from aer.state import StateManager


def _settings(home: Path) -> Settings:
    return Settings(
        home=home,
        store_dir=home / "store",
        cache_dir=home / "cache",
        state_dir=home / "state",
        recipes_dir=home / "recipes",
        profiles_dir=home / "profiles",
        database=home / "database.sqlite3",
        config_file=home / "config.toml",
    )


def test_yaml_specs_never_construct_or_execute_python_objects(tmp_path: Path) -> None:
    marker = tmp_path / "yaml-owned"
    payload = f'!!python/object/apply:os.system ["touch {marker}"]\n'

    build_spec = tmp_path / "build.yaml"
    build_spec.write_text(payload, encoding="utf-8")
    with pytest.raises(AerError) as build_error:
        build_artifact(build_spec, tmp_path / "output.docx")
    assert build_error.value.code == "INVALID_SPEC"

    target = tmp_path / "target.json"
    target.write_text('{"safe":true}', encoding="utf-8")
    patch_spec = tmp_path / "patch.yaml"
    patch_spec.write_text(payload, encoding="utf-8")
    with pytest.raises(AerError) as patch_error:
        apply_patch(target, patch_spec)
    assert patch_error.value.code == "INVALID_PATCH"

    recipe = tmp_path / "recipe.yaml"
    recipe.write_text(payload, encoding="utf-8")
    with pytest.raises(AerError) as recipe_error:
        RecipeEngine(_settings(tmp_path / "home")).validate(recipe)
    assert recipe_error.value.code == "INVALID_SPEC"
    assert not marker.exists()


def test_data_filter_payload_is_data_not_code(tmp_path: Path) -> None:
    marker = tmp_path / "filter-owned"
    source = tmp_path / "rows.csv"
    source.write_text("id,status\n1,pending\n", encoding="utf-8")
    expression = f"status == __import__('os').system('touch {marker}')"
    result = query_data(source, where=[expression])
    assert result.matched_rows == 0
    assert not marker.exists()


def test_command_arguments_with_shell_syntax_are_literal(tmp_path: Path) -> None:
    marker = tmp_path / "shell-owned"
    hostile = f"$(touch {marker}) ; touch {marker}"
    result = run_command(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", hostile],
        timeout=5,
    )
    assert result.ok
    assert result.summary == hostile
    assert not marker.exists()


def test_external_conversion_uses_argv_and_shell_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "pandoc-owned"
    source = tmp_path / f"report;touch {marker.name}.md"
    source.write_text("# Safe")
    output = tmp_path / "report.html"
    observed: dict[str, object] = {}

    class Completed:
        returncode = 0

    def fake_run(argv: list[str], **kwargs: object) -> Completed:
        observed["argv"] = argv
        observed["shell"] = kwargs.get("shell")
        destination = Path(argv[argv.index("-o") + 1])
        destination.write_text("<h1>Safe</h1>")
        return Completed()

    monkeypatch.setattr(conversion_ops.shutil, "which", lambda name: f"/tools/{name}")
    monkeypatch.setattr(conversion_ops.subprocess, "run", fake_run)
    convert_file(source, output)

    assert observed["shell"] is False
    assert observed["argv"] == ["/tools/pandoc", str(source.resolve()), "-o", observed["argv"][3]]
    assert not marker.exists()


def test_markup_conversion_rejects_external_url_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "remote.md"
    source.write_text("![remote](https://example.test/image.png)", encoding="utf-8")
    called = False

    def forbidden_run(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("pandoc must not run")

    monkeypatch.setattr(conversion_ops.shutil, "which", lambda _name: "/tools/pandoc")
    monkeypatch.setattr(conversion_ops.subprocess, "run", forbidden_run)

    with pytest.raises(AerError) as blocked:
        convert_file(source, tmp_path / "remote.html")

    assert blocked.value.code == "UNSUPPORTED_FORMAT"
    assert called is False


def test_archive_traversal_and_expanded_size_limits_are_enforced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../../outside", "owned")
        archive.writestr("manifest.json", '{"version":1,"files":[]}')
    with pytest.raises(AerError) as unsafe:
        verify_archive(traversal)
    assert unsafe.value.code == "PATH_OUTSIDE_ROOT"

    oversized = tmp_path / "oversized.zip"
    with zipfile.ZipFile(oversized, "w") as archive:
        archive.writestr("payload", "x" * 100)
        archive.writestr("manifest.json", '{"version":1,"files":[]}')
    monkeypatch.setattr(archive_ops, "MAX_ZIP_UNCOMPRESSED_BYTES", 50)
    with pytest.raises(AerError) as limit:
        verify_archive(oversized)
    assert limit.value.code == "LIMIT_EXCEEDED"


def test_pdf_split_fan_out_and_output_bytes_are_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "many-pages.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.add_blank_page(width=600, height=800)
    writer.write(source)

    monkeypatch.setattr(pdf_ops, "MAX_PDF_SPLIT_FILES", 1)
    with pytest.raises(AerError) as fan_out:
        split_pdf(source, tmp_path / "fan-out")
    assert fan_out.value.code == "LIMIT_EXCEEDED"
    assert not (tmp_path / "fan-out").exists()

    monkeypatch.setattr(pdf_ops, "MAX_PDF_SPLIT_FILES", 10)
    monkeypatch.setattr(pdf_ops, "MAX_PDF_OUTPUT_BYTES", 64)
    with pytest.raises(AerError) as output_size:
        split_pdf(source, tmp_path / "too-large")
    assert output_size.value.code == "LIMIT_EXCEEDED"
    assert not (tmp_path / "too-large").exists()


def test_state_paths_and_patch_regex_are_bounded(tmp_path: Path) -> None:
    manager = StateManager(_settings(tmp_path / "home"))
    with pytest.raises(AerError) as traversal:
        manager.init("../../escape", "unsafe")
    assert traversal.value.code == "INVALID_ARGUMENT"

    target = tmp_path / "text.txt"
    target.write_text("a" * 100)
    patch = tmp_path / "regex.yaml"
    patch.write_text(
        "version: 1\noperations:\n"
        "  - op: text.regex_replace\n"
        "    pattern: '(a+)+$'\n"
        "    value: x\n",
        encoding="utf-8",
    )
    with pytest.raises(AerError) as regex:
        apply_patch(target, patch)
    assert regex.value.code == "INVALID_PATCH"
    assert target.read_text() == "a" * 100


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support is unavailable")
def test_recipe_path_outside_home_remains_untrusted_through_symlink(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "home")
    external = tmp_path / "external.yaml"
    external.write_text(
        "version: 1\nname: external\nsteps:\n"
        "  - id: store\n"
        "    uses: store.put\n"
        "    with: {source: /tmp/source}\n",
        encoding="utf-8",
    )
    settings.ensure()
    linked = settings.recipes_dir / "linked.yaml"
    linked.symlink_to(external)
    with pytest.raises(AerError) as untrusted:
        RecipeEngine(settings).run(linked, variables={}, dry_run=True)
    assert untrusted.value.code == "UNTRUSTED_RECIPE"

    with pytest.raises(AerError) as named_untrusted:
        RecipeEngine(settings).run("linked", variables={}, dry_run=True)
    assert named_untrusted.value.code == "UNTRUSTED_RECIPE"
