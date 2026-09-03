from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from aer.config import Settings
from aer.errors import AerError
from aer.hashing import normalized_hash
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


def test_state_lifecycle_deduplicates_and_preserves_exact_values(tmp_path: Path) -> None:
    manager = StateManager(_settings(tmp_path / "home"))
    state = manager.init("task-1", "Ship AER v0.1.0 at /exact/path")
    assert state["status"] == "active"
    assert state["goal"] == "Ship AER v0.1.0 at /exact/path"

    manager.update(
        "task-1",
        completed=["build", "build"],
        remaining=["test", "test", "package"],
        decisions={"provider": "TradingView", "amount": "₩43,000"},
        artifacts={"repo": "/home/k4nul/git/agent-efficiency-runtime"},
        warnings=["visual review required", "visual review required"],
    )
    updated = manager.update("task-1", completed=["test"], remaining=["package"])

    assert updated["completed"] == ["build", "test"]
    assert updated["remaining"] == ["package"]
    assert updated["warnings"] == ["visual review required"]
    assert updated["decisions"] == {"provider": "TradingView", "amount": "₩43,000"}
    assert updated["artifacts"]["repo"] == "/home/k4nul/git/agent-efficiency-runtime"
    assert manager.list()[0]["completed_count"] == 2


def test_invalid_update_is_atomic(tmp_path: Path) -> None:
    manager = StateManager(_settings(tmp_path / "home"))
    manager.init("atomic", "Keep original")
    before = manager.show("atomic")

    with pytest.raises(AerError) as caught:
        manager.update("atomic", completed=["must-not-persist"], status="invalid")
    assert caught.value.code == "INVALID_ARGUMENT"
    assert manager.show("atomic") == before


def test_concurrent_updates_do_not_lose_entries(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "home")
    StateManager(settings).init("concurrent", "Concurrent state")

    def update(index: int) -> None:
        StateManager(settings).update("concurrent", completed=[f"item-{index}"])

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(update, range(30)))

    state = StateManager(settings).show("concurrent")
    assert len(state["completed"]) == 30
    assert set(state["completed"]) == {f"item-{index}" for index in range(30)}


def test_checkpoint_hash_and_export_are_reopenable(tmp_path: Path) -> None:
    manager = StateManager(_settings(tmp_path / "home"))
    manager.init("checkpoint", "Checkpoint state")
    manager.update(
        "checkpoint",
        completed=["one"],
        remaining=["two"],
        decisions={"sha": "0123456789abcdef"},
    )
    checkpoint = manager.checkpoint("checkpoint")
    without_hash = {key: value for key, value in checkpoint.items() if key != "sha256"}
    assert checkpoint["sha256"] == normalized_hash(without_hash)

    output = tmp_path / "exports" / "state.yaml"
    result = manager.export("checkpoint", output)
    exported = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert exported == manager.show("checkpoint")
    assert result["sha256"] == normalized_hash(exported)


def test_state_rejects_traversal_conflicts_and_corruption(tmp_path: Path) -> None:
    manager = StateManager(_settings(tmp_path / "home"))
    for task_id in ("../escape", "/absolute", "task/child"):
        with pytest.raises(AerError) as unsafe:
            manager.init(task_id, "unsafe")
        assert unsafe.value.code == "INVALID_ARGUMENT"

    manager.init("existing", "goal")
    with pytest.raises(AerError) as duplicate:
        manager.init("existing", "other")
    assert duplicate.value.code == "CONFLICT"

    path = manager.settings.state_dir / "broken.yaml"
    path.write_text("version: 1\ntask_id: wrong\n", encoding="utf-8")
    with pytest.raises(AerError) as corrupt:
        manager.show("broken")
    assert corrupt.value.code == "CORRUPT_FILE"
    assert all(item["task_id"] != "broken" for item in manager.list())

    cyclic = manager.settings.state_dir / "cyclic.yaml"
    cyclic.write_text(
        "version: 1\n"
        "task_id: cyclic\n"
        "goal: unsafe\n"
        "status: active\n"
        "created_at: now\n"
        "updated_at: now\n"
        "completed: &items [*items]\n"
        "remaining: []\n"
        "decisions: {}\n"
        "artifacts: {}\n"
        "warnings: []\n"
        "checkpoints: []\n",
        encoding="utf-8",
    )
    with pytest.raises(AerError) as cyclic_error:
        manager.show("cyclic")
    assert cyclic_error.value.code == "CORRUPT_FILE"
