"""Runtime configuration and AER_HOME directory management."""

from __future__ import annotations

import os
import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path


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
        config_file = home / "config.toml"
        if config_file.is_file():
            with config_file.open("rb") as handle:
                data = tomllib.load(handle)
            configured_home = data.get("home")
            if configured_home and not configured:
                home = Path(str(configured_home)).expanduser()
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
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (
            self.store_dir,
            self.cache_dir,
            self.state_dir,
            self.recipes_dir,
            self.profiles_dir,
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
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
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write("version = 1\n")

    def connect(self) -> sqlite3.Connection:
        self.ensure()
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
