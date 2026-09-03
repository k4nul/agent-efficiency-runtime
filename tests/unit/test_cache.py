from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

from aer.cache import ContentHashCache, make_cache_key
from aer.config import Settings
from aer.errors import AerError


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


def test_cache_key_includes_correctness_inputs() -> None:
    base = make_cache_key(
        "inspect.summary",
        "1",
        "input-digest",
        spec_hash="spec-digest",
        configuration={"limit": 20},
        dependency_versions={"aer": "0.1.0"},
    )
    same = make_cache_key(
        "inspect.summary",
        "1",
        "input-digest",
        spec_hash="spec-digest",
        configuration={"limit": 20},
        dependency_versions={"aer": "0.1.0"},
    )
    invalidated = make_cache_key(
        "inspect.summary",
        "1",
        "input-digest",
        spec_hash="spec-digest",
        configuration={"limit": 21},
        dependency_versions={"aer": "0.1.0"},
    )

    assert base == same
    assert base != invalidated
    assert len(base) == 64


def test_cache_put_get_and_namespace_separation(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "aer")
    cache = ContentHashCache(settings)
    key = cache.key_for("data.query", "1", "abc", configuration={"limit": 20})

    entry = cache.put(
        key,
        b"result",
        filename="result.json",
        metadata={"rows": 3},
    )

    assert entry.metadata == {"rows": 3}
    assert cache.get(key) is not None
    assert cache.get_bytes(key) == b"result"
    assert cache.get("0" * 64) is None
    assert len(cache.store.list()) == 1
    assert (settings.store_dir / "sha256").exists() is False


def test_replacing_key_reclaims_unreferenced_content(tmp_path: Path) -> None:
    cache = ContentHashCache(settings_for(tmp_path / "aer"))
    key = cache.key_for("build", "1", "input")
    old = cache.put(key, b"old")
    new = cache.put(key, b"new")

    assert old.ref != new.ref
    assert cache.get_bytes(key) == b"new"
    assert len(cache.store.list()) == 1
    with pytest.raises(AerError) as missing:
        cache.store.stat(old.ref)
    assert missing.value.code == "NOT_FOUND"


def test_shared_content_survives_until_last_mapping_is_deleted(tmp_path: Path) -> None:
    cache = ContentHashCache(settings_for(tmp_path / "aer"))
    first_key = cache.key_for("query", "1", "first")
    second_key = cache.key_for("query", "1", "second")
    first = cache.put(first_key, b"shared")
    second = cache.put(second_key, b"shared")
    assert first.ref == second.ref

    assert cache.delete(first_key) is True
    assert cache.get_bytes(second_key) == b"shared"
    assert cache.delete(first_key) is False
    assert cache.delete(second_key) is True
    assert cache.store.list() == []


def test_concurrent_key_replacement_never_leaves_a_stale_mapping(tmp_path: Path) -> None:
    cache = ContentHashCache(settings_for(tmp_path / "aer"))
    key = cache.key_for("build", "1", "shared-input")
    payloads = [f"payload-{index}".encode() for index in range(16)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        entries = list(executor.map(lambda payload: cache.put(key, payload), payloads))

    current = cache.get_bytes(key)
    assert current in payloads
    assert cache.get(key) is not None
    assert len(cache.list()) == 1
    assert len(cache.store.list()) == 1
    assert entries


def test_expired_entry_is_a_miss_and_is_removed(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "aer")
    cache = ContentHashCache(settings)
    key = cache.key_for("convert", "1", "input")
    cache.put(key, b"temporary", ttl=timedelta(hours=1))
    with settings.connect() as connection:
        connection.execute(
            "UPDATE aer_cache_entries SET expires_at = ? WHERE cache_key = ?",
            ("2000-01-01T00:00:00.000000Z", key),
        )

    assert cache.get(key) is None
    assert cache.list() == []
    assert cache.store.list() == []


def test_clear_and_invalid_inputs(tmp_path: Path) -> None:
    cache = ContentHashCache(settings_for(tmp_path / "aer"))
    first_key = cache.key_for("inspect", "1", "one")
    second_key = cache.key_for("inspect", "1", "two")
    cache.put(first_key, b"one")
    cache.put(second_key, b"two")

    assert cache.clear() == 2
    assert cache.list() == []
    assert cache.store.list() == []

    with pytest.raises(AerError) as invalid_key:
        cache.get("../escape")
    assert invalid_key.value.code == "INVALID_ARGUMENT"
    with pytest.raises(AerError) as invalid_ttl:
        cache.put(first_key, b"x", ttl=timedelta(0))
    assert invalid_ttl.value.code == "INVALID_ARGUMENT"
