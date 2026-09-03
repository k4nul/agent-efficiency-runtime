"""Runtime configuration and AER_HOME directory management."""

from __future__ import annotations

import os
import sqlite3
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

from aer.errors import AerError

_MAX_CONFIG_BYTES = 64 * 1024


def _reject_shared_writable(
    path: Path,
    *,
    operation: str,
    label: str,
    path_stat: os.stat_result | None = None,
) -> None:
    if os.name != "posix":
        return
    mode = stat.S_IMODE((path_stat or path.lstat()).st_mode)
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise AerError(
            "INVALID_ARGUMENT",
            f"{label} cannot be writable by group or other users.",
            operation,
            str(path),
            {"mode": oct(mode)},
            "Use a caller-owned path without group or other write permission.",
        )


def _database_file_exists(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(path_stat.st_mode):
        raise AerError(
            "INVALID_ARGUMENT",
            "AER database files cannot be symbolic links.",
            "config.connect",
            str(path),
        )
    if not stat.S_ISREG(path_stat.st_mode):
        raise AerError(
            "INVALID_ARGUMENT",
            "AER database path must be a regular file.",
            "config.connect",
            str(path),
        )
    if os.name == "posix" and stat.S_IMODE(path_stat.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
        raise AerError(
            "INVALID_ARGUMENT",
            "AER database file cannot be writable by group or other users.",
            "config.connect",
            str(path),
            {"mode": oct(stat.S_IMODE(path_stat.st_mode))},
            "Use a caller-owned path without group or other write permission.",
        )
    return True


@dataclass(frozen=True, slots=True)
class Settings:
    home: Path
    store_dir: Path
    cache_dir: Path
    state_dir: Path
    recipes_dir: Path
    profiles_dir: Path
    database: Path
    config_file: Path

    @classmethod
    def load(cls) -> Settings:
        configured = os.environ.get("AER_HOME")
        default_home = Path.home() / ".aer"
        home = Path(configured).expanduser() if configured else default_home
        try:
            home_stat = home.lstat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(home_stat.st_mode):
                raise AerError(
                    "INVALID_ARGUMENT",
                    "AER_HOME cannot be a symbolic link.",
                    "config.load",
                    str(home),
                )
            if not stat.S_ISDIR(home_stat.st_mode):
                raise AerError(
                    "INVALID_ARGUMENT",
                    "AER_HOME must be a directory.",
                    "config.load",
                    str(home),
                )
            _reject_shared_writable(
                home,
                operation="config.load",
                label="AER_HOME",
                path_stat=home_stat,
            )
        config_file = home / "config.toml"
        if config_file.is_file():
            if config_file.is_symlink():
                raise AerError(
                    "INVALID_ARGUMENT",
                    "AER config cannot be a symbolic link.",
                    "config.load",
                    str(config_file),
                )
            _reject_shared_writable(
                config_file,
                operation="config.load",
                label="AER config",
            )
            try:
                size = config_file.stat().st_size
                if size > _MAX_CONFIG_BYTES:
                    raise AerError(
                        "LIMIT_EXCEEDED",
                        "AER config exceeds the size limit.",
                        "config.load",
                        str(config_file),
                        {"bytes": size, "limit": _MAX_CONFIG_BYTES},
                    )
                with config_file.open("rb") as handle:
                    data = tomllib.load(handle)
            except AerError:
                raise
            except tomllib.TOMLDecodeError as exc:
                raise AerError(
                    "CORRUPT_FILE",
                    "AER config is not valid TOML.",
                    "config.load",
                    str(config_file),
                ) from exc
            except OSError as exc:
                raise AerError(
                    "CORRUPT_FILE",
                    "AER config could not be read.",
                    "config.load",
                    str(config_file),
                ) from exc
            version = data.get("version", 1)
            if version != 1:
                raise AerError(
                    "INVALID_SPEC",
                    "Unsupported AER config version.",
                    "config.load",
                    f"{config_file}#/version",
                    {"supported": [1], "actual": version},
                )
            configured_home = data.get("home")
            if configured_home is not None and (
                not isinstance(configured_home, str) or not configured_home.strip()
            ):
                raise AerError(
                    "INVALID_SPEC",
                    "Config home must be a non-empty path string.",
                    "config.load",
                    f"{config_file}#/home",
                )
            if configured_home is not None and not configured:
                home = Path(configured_home).expanduser()
                config_file = home / "config.toml"
        return cls(
            home=home,
            store_dir=home / "store",
            cache_dir=home / "cache",
            state_dir=home / "state",
            recipes_dir=home / "recipes",
            profiles_dir=home / "profiles",
            database=home / "database.sqlite3",
            config_file=config_file,
        )

    def ensure(self) -> None:
        if self.home.is_symlink():
            raise AerError(
                "INVALID_ARGUMENT",
                "AER_HOME cannot be a symbolic link.",
                "config.ensure",
                str(self.home),
            )
        home_existed = self.home.exists()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.home.is_dir():
            raise AerError(
                "INVALID_ARGUMENT", "AER_HOME must be a directory.", "config.ensure", str(self.home)
            )
        _reject_shared_writable(self.home, operation="config.ensure", label="AER_HOME")
        if not home_existed:
            os.chmod(self.home, 0o700)
        for path in (
            self.store_dir,
            self.cache_dir,
            self.state_dir,
            self.recipes_dir,
            self.profiles_dir,
        ):
            if path.is_symlink():
                raise AerError(
                    "INVALID_ARGUMENT",
                    "AER internal directories cannot be symbolic links.",
                    "config.ensure",
                    str(path),
                )
            path_existed = path.exists()
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not path.is_dir():
                raise AerError(
                    "INVALID_ARGUMENT",
                    "AER internal path must be a directory.",
                    "config.ensure",
                    str(path),
                )
            _reject_shared_writable(
                path,
                operation="config.ensure",
                label="AER internal directory",
            )
            if not path_existed:
                os.chmod(path, 0o700)
        if self.config_file.is_symlink():
            raise AerError(
                "INVALID_ARGUMENT",
                "AER config cannot be a symbolic link.",
                "config.ensure",
                str(self.config_file),
            )
        config_created = False
        if not self.config_file.exists():
            try:
                descriptor = os.open(
                    self.config_file,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                pass
            else:
                config_created = True
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write("version = 1\n")
        if self.config_file.is_symlink():
            raise AerError(
                "INVALID_ARGUMENT",
                "AER config cannot be a symbolic link.",
                "config.ensure",
                str(self.config_file),
            )
        if not self.config_file.is_file():
            raise AerError(
                "INVALID_ARGUMENT",
                "AER config path must be a regular file.",
                "config.ensure",
                str(self.config_file),
            )
        _reject_shared_writable(
            self.config_file,
            operation="config.ensure",
            label="AER config",
        )
        if config_created:
            os.chmod(self.config_file, 0o600)

    def connect(self) -> sqlite3.Connection:
        self.ensure()
        database_paths = (
            self.database,
            Path(f"{self.database}-journal"),
            Path(f"{self.database}-wal"),
            Path(f"{self.database}-shm"),
        )
        existed: dict[Path, bool] = {}
        for path in database_paths:
            existed[path] = _database_file_exists(path)
        if not existed[self.database]:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.database, flags, 0o600)
            except FileExistsError:
                existed[self.database] = _database_file_exists(self.database)
            else:
                try:
                    if os.name == "posix":
                        os.fchmod(descriptor, 0o600)
                finally:
                    os.close(descriptor)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.database, timeout=30)
            connection.row_factory = sqlite3.Row
            journal_row = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
            journal_mode = str(journal_row[0]).casefold() if journal_row else None
            if journal_mode != "delete":
                raise AerError(
                    "CONFLICT",
                    "SQLite rollback journal mode could not be activated.",
                    "config.connect",
                    str(self.database),
                    {"requested": "delete", "actual": journal_mode},
                    "Close other processes using the AER database and retry.",
                )
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        except BaseException:
            if connection is not None:
                connection.close()
            raise
