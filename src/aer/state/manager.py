from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from filelock import FileLock

from aer.config import Settings
from aer.errors import AerError
from aer.hashing import normalized_hash
from aer.paths import atomic_write_text, prepare_output_path
from aer.yaml_safety import load_yaml_safely

TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _unique_extend(target: list[str], values: list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _valid_state(value: object, task_id: str) -> bool:
    if not isinstance(value, dict):
        return False
    string_fields = ("task_id", "goal", "status", "created_at", "updated_at")
    list_fields = ("completed", "remaining", "warnings")
    mapping_fields = ("decisions", "artifacts")
    return (
        value.get("version") == 1
        and value.get("task_id") == task_id
        and all(isinstance(value.get(field), str) for field in string_fields)
        and value.get("status") in {"active", "complete", "blocked"}
        and all(
            isinstance(value.get(field), list)
            and all(isinstance(item, str) for item in value[field])
            for field in list_fields
        )
        and all(
            isinstance(value.get(field), dict)
            and all(
                isinstance(key, str) and isinstance(item, str) for key, item in value[field].items()
            )
            for field in mapping_fields
        )
        and isinstance(value.get("checkpoints"), list)
        and all(isinstance(item, dict) for item in value["checkpoints"])
    )


class StateManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.load()
        self.settings.ensure()

    def _path(self, task_id: str) -> Path:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise AerError(
                "INVALID_ARGUMENT", "Task ID contains unsafe characters.", "state", task_id
            )
        return self.settings.state_dir / f"{task_id}.yaml"

    def _lock(self, task_id: str) -> FileLock:
        return FileLock(str(self.settings.state_dir / f".{task_id}.lock"), timeout=30)

    def init(self, task_id: str, goal: str) -> dict[str, Any]:
        if not goal.strip():
            raise AerError("INVALID_ARGUMENT", "Task goal cannot be empty.", "state.init", task_id)
        path = self._path(task_id)
        with self._lock(task_id):
            if path.exists():
                raise AerError("CONFLICT", "Task state already exists.", "state.init", task_id)
            timestamp = _now()
            state: dict[str, Any] = {
                "version": 1,
                "task_id": task_id,
                "goal": goal,
                "status": "active",
                "created_at": timestamp,
                "updated_at": timestamp,
                "completed": [],
                "remaining": [],
                "decisions": {},
                "artifacts": {},
                "warnings": [],
                "checkpoints": [],
            }
            self._write(path, state)
        return state

    def _read(self, path: Path, task_id: str) -> dict[str, Any]:
        if path.is_symlink():
            raise AerError(
                "INVALID_ARGUMENT", "Task state cannot be a symbolic link.", "state", task_id
            )
        if not path.is_file():
            raise AerError("NOT_FOUND", "Task state does not exist.", "state", task_id)
        try:
            value = load_yaml_safely(
                path.read_text(encoding="utf-8"), operation="state", target=task_id
            )
        except AerError as exc:
            if exc.code == "LIMIT_EXCEEDED":
                raise
            raise AerError("CORRUPT_FILE", exc.message, "state", task_id, exc.details) from exc
        except OSError as exc:
            raise AerError(
                "CORRUPT_FILE", f"Cannot read task state: {exc}", "state", task_id
            ) from exc
        if not _valid_state(value, task_id):
            raise AerError("CORRUPT_FILE", "Task state schema is invalid.", "state", task_id)
        assert isinstance(value, dict)
        return value

    @staticmethod
    def _write(path: Path, state: dict[str, Any]) -> None:
        atomic_write_text(path, yaml.safe_dump(state, allow_unicode=True, sort_keys=False))

    def show(self, task_id: str) -> dict[str, Any]:
        return self._read(self._path(task_id), task_id)

    def update(
        self,
        task_id: str,
        *,
        completed: list[str] | None = None,
        remaining: list[str] | None = None,
        decisions: dict[str, str] | None = None,
        artifacts: dict[str, str] | None = None,
        warnings: list[str] | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        path = self._path(task_id)
        with self._lock(task_id):
            state = self._read(path, task_id)
            _unique_extend(state["completed"], completed or [])
            _unique_extend(state["remaining"], remaining or [])
            _unique_extend(state["warnings"], warnings or [])
            for item in completed or []:
                if item in state["remaining"]:
                    state["remaining"].remove(item)
            state["decisions"].update(decisions or {})
            state["artifacts"].update(artifacts or {})
            if status:
                if status not in {"active", "complete", "blocked"}:
                    raise AerError(
                        "INVALID_ARGUMENT",
                        "State status must be active, complete, or blocked.",
                        "state.update",
                        status,
                    )
                state["status"] = status
            state["updated_at"] = _now()
            self._write(path, state)
        return state

    def list(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1000:
            raise AerError(
                "INVALID_ARGUMENT", "State list limit must be between 1 and 1000.", "state.list"
            )
        records = []
        for path in sorted(
            self.settings.state_dir.glob("*.yaml"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            try:
                state = self._read(path, path.stem)
            except AerError:
                continue
            records.append(
                {
                    "task_id": state["task_id"],
                    "goal": state["goal"],
                    "status": state["status"],
                    "updated_at": state["updated_at"],
                    "completed_count": len(state["completed"]),
                    "remaining_count": len(state["remaining"]),
                }
            )
            if len(records) >= limit:
                break
        return records

    def checkpoint(self, task_id: str) -> dict[str, Any]:
        path = self._path(task_id)
        with self._lock(task_id):
            state = self._read(path, task_id)
            snapshot = {
                "created_at": _now(),
                "status": state["status"],
                "completed": list(state["completed"]),
                "remaining": list(state["remaining"]),
                "decisions": dict(state["decisions"]),
                "artifacts": dict(state["artifacts"]),
            }
            snapshot["sha256"] = normalized_hash(snapshot)
            state["checkpoints"].append(snapshot)
            state["updated_at"] = _now()
            self._write(path, state)
        return snapshot

    def export(self, task_id: str, output: Path | None = None) -> dict[str, Any]:
        state = self.show(task_id)
        if output is not None:
            destination = prepare_output_path(output, operation="state.export")
            self._write(destination, state)
            return {
                "task_id": task_id,
                "output": str(destination),
                "sha256": normalized_hash(state),
            }
        return state
