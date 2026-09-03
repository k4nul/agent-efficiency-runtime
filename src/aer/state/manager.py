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
_MAX_STATE_FILE_BYTES = 4 * 1024 * 1024
_MAX_STATE_VALUE_BYTES = 64 * 1024
_MAX_STATE_UPDATE_ITEMS = 10_000


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _unique_extend(target: list[str], values: list[str]) -> None:
    existing = set(target)
    for value in values:
        if value not in existing:
            target.append(value)
            existing.add(value)


def _bounded_text(value: object, *, field: str, operation: str) -> str:
    if not isinstance(value, str):
        raise AerError(
            "INVALID_ARGUMENT",
            f"State {field} values must be strings.",
            operation,
            field,
        )
    size = len(value.encode("utf-8"))
    if size > _MAX_STATE_VALUE_BYTES:
        raise AerError(
            "LIMIT_EXCEEDED",
            f"State {field} value exceeds the size limit.",
            operation,
            field,
            {"bytes": size, "limit": _MAX_STATE_VALUE_BYTES},
        )
    return value


def _validate_update_inputs(
    *,
    completed: list[str] | None,
    remaining: list[str] | None,
    decisions: dict[str, str] | None,
    artifacts: dict[str, str] | None,
    warnings: list[str] | None,
) -> None:
    lists: tuple[tuple[str, object], ...] = (
        ("completed", completed),
        ("remaining", remaining),
        ("warnings", warnings),
    )
    mappings: tuple[tuple[str, object], ...] = (
        ("decisions", decisions),
        ("artifacts", artifacts),
    )
    item_count = 0
    for field, values in lists:
        if values is None:
            continue
        if not isinstance(values, list):
            raise AerError(
                "INVALID_ARGUMENT",
                f"State {field} must be a string array.",
                "state.update",
                field,
            )
        item_count += len(values)
    for field, values in mappings:
        if values is None:
            continue
        if not isinstance(values, dict):
            raise AerError(
                "INVALID_ARGUMENT",
                f"State {field} must be a string mapping.",
                "state.update",
                field,
            )
        item_count += len(values)
    if item_count > _MAX_STATE_UPDATE_ITEMS:
        raise AerError(
            "LIMIT_EXCEEDED",
            "State update exceeds the item-count limit.",
            "state.update",
            details={"items": item_count, "limit": _MAX_STATE_UPDATE_ITEMS},
        )

    total_bytes = 0
    for field, values in lists:
        if values is None:
            continue
        assert isinstance(values, list)
        for value in values:
            total_bytes += len(
                _bounded_text(value, field=field, operation="state.update").encode("utf-8")
            )
    for field, values in mappings:
        if values is None:
            continue
        assert isinstance(values, dict)
        for key, value in values.items():
            total_bytes += len(
                _bounded_text(key, field=f"{field} key", operation="state.update").encode("utf-8")
            )
            total_bytes += len(
                _bounded_text(value, field=field, operation="state.update").encode("utf-8")
            )
    if total_bytes > _MAX_STATE_FILE_BYTES:
        raise AerError(
            "LIMIT_EXCEEDED",
            "State update exceeds the byte-size limit.",
            "state.update",
            details={
                "bytes": total_bytes,
                "limit": _MAX_STATE_FILE_BYTES,
            },
        )


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
        return FileLock(
            str(self.settings.state_dir / f".{task_id}.lock"),
            timeout=30,
            mode=0o600,
            preserve_lock_file=True,
            fallback_to_soft=False,
        )

    def init(self, task_id: str, goal: str) -> dict[str, Any]:
        clean_goal = _bounded_text(goal, field="goal", operation="state.init")
        if not clean_goal.strip():
            raise AerError("INVALID_ARGUMENT", "Task goal cannot be empty.", "state.init", task_id)
        path = self._path(task_id)
        with self._lock(task_id):
            if path.exists():
                raise AerError("CONFLICT", "Task state already exists.", "state.init", task_id)
            timestamp = _now()
            state: dict[str, Any] = {
                "version": 1,
                "task_id": task_id,
                "goal": clean_goal,
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
            self._write(path, state, operation="state.init")
        return state

    def _read(self, path: Path, task_id: str) -> dict[str, Any]:
        if path.is_symlink():
            raise AerError(
                "INVALID_ARGUMENT", "Task state cannot be a symbolic link.", "state", task_id
            )
        if not path.is_file():
            raise AerError("NOT_FOUND", "Task state does not exist.", "state", task_id)
        try:
            with path.open("rb") as handle:
                encoded = handle.read(_MAX_STATE_FILE_BYTES + 1)
            if len(encoded) > _MAX_STATE_FILE_BYTES:
                raise AerError(
                    "LIMIT_EXCEEDED",
                    "Task state exceeds the file size limit.",
                    "state",
                    task_id,
                    {"bytes_at_least": len(encoded), "limit": _MAX_STATE_FILE_BYTES},
                )
            value = load_yaml_safely(encoded.decode("utf-8"), operation="state", target=task_id)
        except AerError as exc:
            if exc.code == "LIMIT_EXCEEDED":
                raise
            raise AerError("CORRUPT_FILE", exc.message, "state", task_id, exc.details) from exc
        except (OSError, UnicodeError) as exc:
            raise AerError(
                "CORRUPT_FILE", f"Cannot read task state: {exc}", "state", task_id
            ) from exc
        if not _valid_state(value, task_id):
            raise AerError("CORRUPT_FILE", "Task state schema is invalid.", "state", task_id)
        assert isinstance(value, dict)
        return value

    @staticmethod
    def _write(path: Path, state: dict[str, Any], *, operation: str) -> None:
        task_id = str(state.get("task_id", path.stem))
        if not _valid_state(state, task_id):
            raise AerError("INVALID_ARGUMENT", "Task state schema is invalid.", operation, task_id)
        serialized = yaml.safe_dump(state, allow_unicode=True, sort_keys=False)
        size = len(serialized.encode("utf-8"))
        if size > _MAX_STATE_FILE_BYTES:
            raise AerError(
                "LIMIT_EXCEEDED",
                "Task state exceeds the file size limit.",
                operation,
                task_id,
                {"bytes": size, "limit": _MAX_STATE_FILE_BYTES},
            )
        # Keep every manager-written file inside the same parser bounds used on read.
        load_yaml_safely(serialized, operation=operation, target=task_id)
        atomic_write_text(path, serialized)

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
        _validate_update_inputs(
            completed=completed,
            remaining=remaining,
            decisions=decisions,
            artifacts=artifacts,
            warnings=warnings,
        )
        if status is not None and status not in {"active", "complete", "blocked"}:
            raise AerError(
                "INVALID_ARGUMENT",
                "State status must be active, complete, or blocked.",
                "state.update",
                status,
            )
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
            if status is not None:
                state["status"] = status
            state["updated_at"] = _now()
            self._write(path, state, operation="state.update")
        return state

    def list(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1000:
            raise AerError(
                "INVALID_ARGUMENT", "State list limit must be between 1 and 1000.", "state.list"
            )
        records = []
        candidates: list[tuple[float, Path]] = []
        for path in self.settings.state_dir.glob("*.yaml"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                candidates.append((path.stat(follow_symlinks=False).st_mtime, path))
            except OSError:
                continue
        for _mtime, path in sorted(candidates, key=lambda item: item[0], reverse=True):
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
            self._write(path, state, operation="state.checkpoint")
        return snapshot

    def export(self, task_id: str, output: Path | None = None) -> dict[str, Any]:
        state = self.show(task_id)
        if output is not None:
            destination = prepare_output_path(output, operation="state.export")
            self._write(destination, state, operation="state.export")
            return {
                "task_id": task_id,
                "output": str(destination),
                "sha256": normalized_hash(state),
            }
        return state
