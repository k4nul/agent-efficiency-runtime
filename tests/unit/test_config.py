from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import aer.config as config_module
from aer.config import Settings
from aer.errors import AerError


def test_config_load_rejects_malformed_oversized_and_unknown_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "aer"
    home.mkdir()
    home.chmod(0o700)
    config = home / "config.toml"
    monkeypatch.setenv("AER_HOME", str(home))

    config.write_text("not = [valid", encoding="utf-8")
    config.chmod(0o640)
    with pytest.raises(AerError) as malformed:
        Settings.load()
    assert malformed.value.code == "CORRUPT_FILE"

    config.write_bytes(b"x" * (config_module._MAX_CONFIG_BYTES + 1))
    with pytest.raises(AerError) as oversized:
        Settings.load()
    assert oversized.value.code == "LIMIT_EXCEEDED"

    config.write_text("version = 2\n", encoding="utf-8")
    with pytest.raises(AerError) as version:
        Settings.load()
    assert version.value.code == "INVALID_SPEC"

    config.write_text("version = 1\nhome = 42\n", encoding="utf-8")
    with pytest.raises(AerError) as invalid_home:
        Settings.load()
    assert invalid_home.value.code == "INVALID_SPEC"


def test_aer_home_environment_wins_over_config_redirect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configured = tmp_path / "configured"
    configured.mkdir()
    configured.chmod(0o700)
    config = configured / "config.toml"
    config.write_text(f'version = 1\nhome = "{tmp_path / "redirected"}"\n', encoding="utf-8")
    config.chmod(0o640)
    monkeypatch.setenv("AER_HOME", str(configured))

    assert Settings.load().home == configured


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_load_rejects_shared_writable_config_before_home_redirect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    default_home = tmp_path / ".aer"
    redirected = tmp_path / "redirected"
    default_home.mkdir()
    default_home.chmod(0o700)
    config = default_home / "config.toml"
    config.write_text(f'version = 1\nhome = "{redirected}"\n', encoding="utf-8")
    config.chmod(0o660)
    monkeypatch.delenv("AER_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))

    with pytest.raises(AerError) as captured:
        Settings.load()

    assert captured.value.code == "INVALID_ARGUMENT"
    assert captured.value.target == str(config)
    assert captured.value.operation == "config.load"
    assert not redirected.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_load_rejects_shared_writable_home_before_config_redirect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    default_home = tmp_path / ".aer"
    redirected = tmp_path / "redirected"
    default_home.mkdir()
    default_home.chmod(0o770)
    config = default_home / "config.toml"
    config.write_text(f'version = 1\nhome = "{redirected}"\n', encoding="utf-8")
    config.chmod(0o640)
    monkeypatch.delenv("AER_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))

    with pytest.raises(AerError) as captured:
        Settings.load()

    assert captured.value.code == "INVALID_ARGUMENT"
    assert captured.value.target == str(default_home)
    assert captured.value.operation == "config.load"
    assert not redirected.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_ensure_restricts_runtime_files_and_rejects_internal_symlink(tmp_path: Path) -> None:
    home = tmp_path / "aer"
    settings = Settings(
        home=home,
        store_dir=home / "store",
        cache_dir=home / "cache",
        state_dir=home / "state",
        recipes_dir=home / "recipes",
        profiles_dir=home / "profiles",
        database=home / "database.sqlite3",
        config_file=home / "config.toml",
    )
    settings.ensure()

    with settings.connect() as connection:
        connection.execute("CREATE TABLE permission_probe (value INTEGER)")

    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE(settings.config_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.database.stat().st_mode) == 0o600

    settings.store_dir.rmdir()
    settings.store_dir.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(AerError) as symlink:
        settings.ensure()
    assert symlink.value.code == "INVALID_ARGUMENT"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_ensure_does_not_chmod_existing_custom_paths(tmp_path: Path) -> None:
    home = tmp_path / "shared-home"
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    store = home / "store"
    store.mkdir(mode=0o750)
    config = home / "config.toml"
    config.write_text("version = 1\n", encoding="utf-8")
    config.chmod(0o640)
    settings = Settings(
        home=home,
        store_dir=store,
        cache_dir=home / "cache",
        state_dir=home / "state",
        recipes_dir=home / "recipes",
        profiles_dir=home / "profiles",
        database=home / "database.sqlite3",
        config_file=config,
    )

    settings.ensure()

    assert stat.S_IMODE(home.stat().st_mode) == 0o755
    assert stat.S_IMODE(store.stat().st_mode) == 0o750
    assert stat.S_IMODE(config.stat().st_mode) == 0o640


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
@pytest.mark.parametrize("mode", [0o770, 0o707])
def test_ensure_rejects_shared_writable_home_without_changing_it(tmp_path: Path, mode: int) -> None:
    home = tmp_path / "shared-writable"
    home.mkdir(mode=mode)
    home.chmod(mode)
    settings = Settings(
        home=home,
        store_dir=home / "store",
        cache_dir=home / "cache",
        state_dir=home / "state",
        recipes_dir=home / "recipes",
        profiles_dir=home / "profiles",
        database=home / "database.sqlite3",
        config_file=home / "config.toml",
    )

    with pytest.raises(AerError) as captured:
        settings.ensure()

    assert captured.value.code == "INVALID_ARGUMENT"
    assert stat.S_IMODE(home.stat().st_mode) == mode
    assert list(home.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
@pytest.mark.parametrize("target_name", ["store", "config.toml"])
@pytest.mark.parametrize("mode", [0o770, 0o707])
def test_ensure_rejects_shared_writable_internal_paths_without_chmod(
    tmp_path: Path, target_name: str, mode: int
) -> None:
    home = tmp_path / "aer"
    settings = Settings(
        home=home,
        store_dir=home / "store",
        cache_dir=home / "cache",
        state_dir=home / "state",
        recipes_dir=home / "recipes",
        profiles_dir=home / "profiles",
        database=home / "database.sqlite3",
        config_file=home / "config.toml",
    )
    settings.ensure()
    target = home / target_name
    target.chmod(mode if target.is_dir() else mode & 0o666)

    with pytest.raises(AerError) as captured:
        settings.ensure()

    assert captured.value.code == "INVALID_ARGUMENT"
    assert captured.value.target == str(target)
    assert stat.S_IMODE(target.stat().st_mode) & (stat.S_IWGRP | stat.S_IWOTH)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
@pytest.mark.parametrize("mode", [0o660, 0o606])
def test_connect_rejects_shared_writable_database_without_chmod(tmp_path: Path, mode: int) -> None:
    home = tmp_path / "aer"
    settings = Settings(
        home=home,
        store_dir=home / "store",
        cache_dir=home / "cache",
        state_dir=home / "state",
        recipes_dir=home / "recipes",
        profiles_dir=home / "profiles",
        database=home / "database.sqlite3",
        config_file=home / "config.toml",
    )
    settings.ensure()
    settings.database.write_bytes(b"")
    settings.database.chmod(mode)

    with pytest.raises(AerError) as captured:
        settings.connect()

    assert captured.value.code == "INVALID_ARGUMENT"
    assert stat.S_IMODE(settings.database.stat().st_mode) == mode


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_connect_rejects_database_symlinks(tmp_path: Path, suffix: str) -> None:
    home = tmp_path / "aer"
    settings = Settings(
        home=home,
        store_dir=home / "store",
        cache_dir=home / "cache",
        state_dir=home / "state",
        recipes_dir=home / "recipes",
        profiles_dir=home / "profiles",
        database=home / "database.sqlite3",
        config_file=home / "config.toml",
    )
    settings.ensure()
    outside = tmp_path / f"outside{suffix or '-database'}"
    outside.write_bytes(b"unchanged")
    database_path = Path(f"{settings.database}{suffix}")
    database_path.symlink_to(outside)

    with pytest.raises(AerError) as captured:
        settings.connect()

    assert captured.value.code == "INVALID_ARGUMENT"
    assert outside.read_bytes() == b"unchanged"


def test_connect_rejects_unavailable_rollback_journal_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "aer"
    settings = Settings(
        home=home,
        store_dir=home / "store",
        cache_dir=home / "cache",
        state_dir=home / "state",
        recipes_dir=home / "recipes",
        profiles_dir=home / "profiles",
        database=home / "database.sqlite3",
        config_file=home / "config.toml",
    )
    connection = MagicMock()
    journal_cursor = MagicMock()
    journal_cursor.fetchone.return_value = ("wal",)
    connection.execute.side_effect = lambda sql: (
        journal_cursor if sql == "PRAGMA journal_mode=DELETE" else MagicMock()
    )
    monkeypatch.setattr(config_module.sqlite3, "connect", lambda *_args, **_kwargs: connection)

    with pytest.raises(AerError) as captured:
        settings.connect()

    assert captured.value.code == "CONFLICT"
    assert captured.value.details == {"requested": "delete", "actual": "wal"}
    connection.close.assert_called_once_with()
