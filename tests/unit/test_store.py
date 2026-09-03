from __future__ import annotations

import io
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

from aer.config import Settings
from aer.errors import AerError
from aer.store import ObjectStore, format_ref, parse_ref


def settings_for(home: Path) -> Settings:
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


def put_in_process(arguments: tuple[Settings, int]) -> str:
    settings, index = arguments
    return ObjectStore(settings).put_bytes(b"process payload", filename=f"process-{index}.txt").ref


def test_put_deduplicates_and_records_metadata(tmp_path: Path) -> None:
    store = ObjectStore(settings_for(tmp_path / "aer"))

    first = store.put_bytes(
        b"same content",
        filename="report.txt",
        source={"task": "test"},
    )
    second = store.put_bytes(b"same content", filename="copy.txt")

    assert first.ref == second.ref
    assert parse_ref(first.ref) == first.digest
    assert format_ref(first.digest) == first.ref
    assert second.filename == "copy.txt"
    assert second.mime_type == "text/plain"
    assert second.size == 12
    assert second.source == {"task": "test"}
    assert store.get_bytes(first.ref) == b"same content"
    assert len(store.list()) == 1
    assert len(list((tmp_path / "aer" / "store" / "sha256").rglob(first.digest))) == 1


def test_put_file_stdin_get_and_cat_are_bounded(tmp_path: Path) -> None:
    store = ObjectStore(settings_for(tmp_path / "aer"))
    source = tmp_path / "input.txt"
    source.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    file_record = store.put_file(source)
    assert file_record.filename == "input.txt"
    assert file_record.source["path"] == str(source.resolve())

    result = store.cat(file_record.ref, start_line=2, end_line=3)
    assert result.text == "two\nthree\n"
    assert result.returned_lines == 2
    assert result.truncated is False

    limited = store.cat(file_record.ref, max_bytes=5)
    assert len(limited.text.encode("utf-8")) <= 5
    assert limited.truncated is True
    assert limited.raw_ref == file_record.ref

    with pytest.raises(AerError) as invalid_line:
        store.cat(file_record.ref, start_line=0)
    assert invalid_line.value.code == "INVALID_ARGUMENT"

    stdin_record = store.put_stdin(stream=io.BytesIO(b"stdin"), filename="stdin.txt")
    output = tmp_path / "restored.txt"
    returned = store.get(stdin_record.ref, output)
    assert returned.ref == stdin_record.ref
    assert output.read_bytes() == b"stdin"
    with pytest.raises(AerError, match="already exists") as conflict:
        store.get_to_file(stdin_record.ref, output)
    assert conflict.value.code == "CONFLICT"

    linked_output = tmp_path / "linked-output"
    linked_output.symlink_to(tmp_path / "target-output")
    with pytest.raises(AerError) as symlink_output:
        store.get(stdin_record.ref, linked_output, overwrite=True)
    assert symlink_output.value.code == "INVALID_ARGUMENT"


def test_ref_filename_traversal_and_symlink_are_rejected(tmp_path: Path) -> None:
    store = ObjectStore(settings_for(tmp_path / "aer"))
    with pytest.raises(AerError) as bad_ref:
        store.stat("aer://sha256/../../etc/passwd")
    assert bad_ref.value.code == "INVALID_ARGUMENT"

    with pytest.raises(AerError) as bad_name:
        store.put_bytes(b"x", filename="../escape")
    assert bad_name.value.code == "INVALID_ARGUMENT"

    source = tmp_path / "source"
    source.write_bytes(b"secret")
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(AerError) as symlink_error:
        store.put_file(link)
    assert symlink_error.value.code == "INVALID_ARGUMENT"


def test_database_initialization_lock_symlink_is_rejected(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "aer")
    settings.ensure()
    outside = tmp_path / "outside-lock"
    outside.write_bytes(b"unchanged")
    lock_path = settings.home / ".database-init.lock"
    lock_path.symlink_to(outside)

    with pytest.raises(AerError) as captured:
        ObjectStore(settings)

    assert captured.value.code == "INVALID_ARGUMENT"
    assert captured.value.target == str(lock_path)
    assert outside.read_bytes() == b"unchanged"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_shared_writable_database_initialization_lock_is_rejected(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "aer")
    settings.ensure()
    lock_path = settings.home / ".database-init.lock"
    lock_path.write_bytes(b"")
    lock_path.chmod(0o660)

    with pytest.raises(AerError) as captured:
        ObjectStore(settings)

    assert captured.value.code == "INVALID_ARGUMENT"
    assert captured.value.target == str(lock_path)


def test_stdin_limit_and_binary_cat_fail_compactly(tmp_path: Path) -> None:
    store = ObjectStore(settings_for(tmp_path / "aer"))
    with pytest.raises(AerError) as too_large:
        store.put_stdin(stream=io.BytesIO(b"12345"), max_bytes=4)
    assert too_large.value.code == "LIMIT_EXCEEDED"
    assert list((tmp_path / "aer" / "store" / ".tmp").iterdir()) == []

    binary = store.put_bytes(b"\xff\x00")
    with pytest.raises(AerError) as unsupported:
        store.cat(binary.ref)
    assert unsupported.value.code == "UNSUPPORTED_FORMAT"


def test_verify_detects_corrupted_content(tmp_path: Path) -> None:
    store = ObjectStore(settings_for(tmp_path / "aer"))
    record = store.put_bytes(b"original")
    object_path = next((tmp_path / "aer" / "store" / "sha256").rglob(record.digest))
    object_path.write_bytes(b"tampered")

    with pytest.raises(AerError) as corrupt:
        store.verify(record.ref)
    assert corrupt.value.code == "CORRUPT_FILE"
    assert corrupt.value.details["actual_sha256"] != record.digest


def test_concurrent_put_is_safe_and_deduplicated(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "aer")
    ObjectStore(settings)

    def put_once(index: int) -> str:
        return (
            ObjectStore(settings)
            .put_bytes(
                b"concurrent payload",
                filename=f"input-{index}.txt",
            )
            .ref
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        refs = list(executor.map(put_once, range(24)))

    store = ObjectStore(settings)
    assert len(set(refs)) == 1
    assert store.verify(refs[0]).size == len(b"concurrent payload")
    assert len(store.list()) == 1


def test_concurrent_process_put_is_safe_and_deduplicated(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "aer")
    ObjectStore(settings)
    context = multiprocessing.get_context("spawn")

    with ProcessPoolExecutor(max_workers=4, mp_context=context) as executor:
        refs = list(executor.map(put_in_process, [(settings, index) for index in range(12)]))

    store = ObjectStore(settings)
    assert len(set(refs)) == 1
    assert store.verify(refs[0]).size == len(b"process payload")
    assert len(store.list()) == 1


def test_pin_and_gc_protect_then_delete_only_store_objects(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "aer")
    store = ObjectStore(settings)
    cached = ObjectStore(settings, namespace="cache")
    protected = store.put_bytes(b"keep", pin=True)
    removable = store.put_bytes(b"remove")
    cached_record = cached.put_bytes(b"cache")

    preview = store.gc(older_than=timedelta(seconds=0), dry_run=True)
    assert preview.deleted == 1
    assert preview.sample_refs == (removable.ref,)
    assert store.get_bytes(removable.ref) == b"remove"

    removed = store.gc(older_than=timedelta(seconds=0))
    assert removed.deleted == 1
    assert removed.bytes_reclaimed == len(b"remove")
    assert store.stat(protected.ref).pinned is True
    assert cached.get_bytes(cached_record.ref) == b"cache"
    with pytest.raises(AerError) as missing:
        store.stat(removable.ref)
    assert missing.value.code == "NOT_FOUND"

    store.pin(protected.ref, pinned=False)
    assert store.gc(older_than=timedelta(seconds=0)).deleted == 1


def test_delete_refuses_pinned_object_without_force(tmp_path: Path) -> None:
    store = ObjectStore(settings_for(tmp_path / "aer"))
    record = store.put_bytes(b"important", pin=True)

    with pytest.raises(AerError) as conflict:
        store.delete(record.ref)
    assert conflict.value.code == "CONFLICT"
    assert store.delete(record.ref, force=True) is True
    assert store.delete(record.ref, force=True) is False
