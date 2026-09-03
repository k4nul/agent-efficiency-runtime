"""Measured agent-work profiles stored in the shared SQLite database."""

from __future__ import annotations

import builtins
import csv
import io
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from aer.config import Settings
from aer.errors import AerError
from aer.paths import atomic_write_text

PROFILE_FIELDS: Final[tuple[str, ...]] = (
    "task",
    "variant",
    "timestamp",
    "model",
    "model_calls",
    "tool_calls",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "tool_schema_tokens",
    "tool_result_tokens",
    "retries",
    "duration_ms",
    "success",
    "human_edits",
    "notes",
)

# Provider usage APIs normally report reasoning tokens as part of output tokens and
# tool schemas/results as part of input tokens.  Keep those component fields for
# diagnosis, but do not add them to the provider-style total a second time.
TOKEN_TOTAL_FIELDS: Final[tuple[str, ...]] = ("input_tokens", "output_tokens")

NUMERIC_FIELDS: Final[tuple[str, ...]] = (
    "model_calls",
    "tool_calls",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "tool_schema_tokens",
    "tool_result_tokens",
    "retries",
    "duration_ms",
    "human_edits",
)
_MAX_SQLITE_INTEGER: Final[int] = (1 << 63) - 1


def _timestamp(value: datetime | str | None = None) -> str:
    if value is None:
        parsed = datetime.now(UTC)
    elif isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AerError(
                "INVALID_ARGUMENT",
                "Profile timestamp must be ISO 8601.",
                operation="profile.record",
                target=value,
            ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AerError(
            "INVALID_ARGUMENT",
            "Profile timestamp must include a timezone.",
            operation="profile.record",
        )
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ProfileRecord:
    task: str
    variant: str
    timestamp: str
    model: str | None
    model_calls: int | None
    tool_calls: int | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    tool_schema_tokens: int | None
    tool_result_tokens: int | None
    retries: int | None
    duration_ms: int | None
    success: bool
    human_edits: int | None
    notes: str | None

    @property
    def reported_total_tokens(self) -> int:
        """Return input plus output tokens without double-counting component subsets."""

        return sum(int(getattr(self, field) or 0) for field in TOKEN_TOTAL_FIELDS)

    @property
    def missing_token_fields(self) -> tuple[str, ...]:
        return tuple(field for field in TOKEN_TOTAL_FIELDS if getattr(self, field) is None)

    def as_dict(self) -> dict[str, object]:
        values = {field: getattr(self, field) for field in PROFILE_FIELDS}
        values.update(
            {
                "total_tokens": self.reported_total_tokens,
                "total_tokens_complete": not self.missing_token_fields,
                "missing_token_fields": list(self.missing_token_fields),
                "measurement_source": "caller_supplied_unverified",
                "estimate_classification": "not_recorded",
                "provider_billed_tokens": None,
                "provider_billed_tokens_known": False,
            }
        )
        return values


@dataclass(frozen=True, slots=True)
class AggregateMetrics:
    records: int
    successes: int
    failures: int
    success_rate: float | None
    reported_total_tokens: int
    tokens_per_success: float | None
    tokens_per_success_complete: bool
    model_calls_per_success: float | None
    model_calls_per_success_complete: bool
    tool_calls_per_success: float | None
    tool_calls_per_success_complete: bool
    average_retries: float | None
    average_duration_ms: float | None
    total_duration_ms: int
    average_human_edits: float | None
    field_totals: dict[str, int]
    known_records: dict[str, int]
    missing_records: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return {
            "records": self.records,
            "successes": self.successes,
            "failures": self.failures,
            "success_rate": self.success_rate,
            "total_tokens": self.reported_total_tokens,
            "tokens_per_success": self.tokens_per_success,
            "tokens_per_success_complete": self.tokens_per_success_complete,
            "model_calls_per_success": self.model_calls_per_success,
            "model_calls_per_success_complete": self.model_calls_per_success_complete,
            "tool_calls_per_success": self.tool_calls_per_success,
            "tool_calls_per_success_complete": self.tool_calls_per_success_complete,
            "average_retries": self.average_retries,
            "average_duration_ms": self.average_duration_ms,
            "total_duration_ms": self.total_duration_ms,
            "average_human_edits": self.average_human_edits,
            "field_totals": self.field_totals,
            "known_records": self.known_records,
            "missing_records": self.missing_records,
            "measurement": {
                "source": "caller_supplied_unverified",
                "provenance_verified": False,
                "estimate_classification": "not_recorded",
                "total_token_formula": "input_tokens + output_tokens",
                "cached_input_tokens_counted_separately": True,
                "component_tokens_counted_separately": [
                    "reasoning_tokens",
                    "tool_schema_tokens",
                    "tool_result_tokens",
                ],
                "per_success_includes_failed_attempt_cost": True,
                "missing_values_treated_as_zero_in_reported_subtotals": True,
                "provider_billed_tokens": None,
                "provider_billed_tokens_known": False,
            },
        }


@dataclass(frozen=True, slots=True)
class ProfileReport:
    generated_at: str
    task: str | None
    overall: AggregateMetrics
    variants: dict[str, AggregateMetrics]

    def as_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "task": self.task,
            "overall": self.overall.as_dict(),
            "variants": {name: value.as_dict() for name, value in self.variants.items()},
        }


@dataclass(frozen=True, slots=True)
class ProfileComparison:
    task: str
    variants: dict[str, AggregateMetrics]
    lowest_tokens_per_success_variant: str | None
    differences_from_lowest: dict[str, dict[str, float]]

    def as_dict(self) -> dict[str, object]:
        return {
            "task": self.task,
            "variants": {name: value.as_dict() for name, value in self.variants.items()},
            "lowest_tokens_per_success_variant": self.lowest_tokens_per_success_variant,
            "differences_from_lowest": self.differences_from_lowest,
            "comparison_basis": (
                "all recorded input/output tokens divided by successful task count"
            ),
            "provider_billed_tokens_known": False,
        }


class ProfileStore:
    """Record and aggregate caller-supplied values without asserting their provenance."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.load()
        self.settings.ensure()
        self._initialize_database()

    def record(
        self,
        *,
        task: str,
        variant: str,
        success: bool,
        timestamp: datetime | str | None = None,
        model: str | None = None,
        model_calls: int | None = None,
        tool_calls: int | None = None,
        input_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        output_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        tool_schema_tokens: int | None = None,
        tool_result_tokens: int | None = None,
        retries: int | None = None,
        duration_ms: int | None = None,
        human_edits: int | None = None,
        notes: str | None = None,
    ) -> ProfileRecord:
        clean_task = self._label(task, name="task")
        clean_variant = self._label(variant, name="variant")
        clean_model = None if model is None else self._optional_text(model, name="model", limit=256)
        clean_notes = (
            None if notes is None else self._optional_text(notes, name="notes", limit=8192)
        )
        numeric_values = {
            "model_calls": model_calls,
            "tool_calls": tool_calls,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "tool_schema_tokens": tool_schema_tokens,
            "tool_result_tokens": tool_result_tokens,
            "retries": retries,
            "duration_ms": duration_ms,
            "human_edits": human_edits,
        }
        for field, value in numeric_values.items():
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > _MAX_SQLITE_INTEGER
            ):
                raise AerError(
                    "INVALID_ARGUMENT",
                    f"{field} must be a non-negative 64-bit integer or omitted.",
                    operation="profile.record",
                    target=field,
                )
        if not isinstance(success, bool):
            raise AerError(
                "INVALID_ARGUMENT",
                "success must be a boolean.",
                operation="profile.record",
                target="success",
            )
        record = ProfileRecord(
            task=clean_task,
            variant=clean_variant,
            timestamp=_timestamp(timestamp),
            model=clean_model,
            model_calls=model_calls,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            tool_schema_tokens=tool_schema_tokens,
            tool_result_tokens=tool_result_tokens,
            retries=retries,
            duration_ms=duration_ms,
            success=success,
            human_edits=human_edits,
            notes=clean_notes,
        )
        placeholders = ", ".join("?" for _ in PROFILE_FIELDS)
        columns = ", ".join(PROFILE_FIELDS)
        with self.settings.connect() as connection:
            connection.execute(
                f"INSERT INTO aer_profiles ({columns}) VALUES ({placeholders})",
                tuple(
                    int(bool(value)) if field == "success" else value
                    for field, value in self._items(record)
                ),
            )
        return record

    def list(
        self,
        *,
        task: str | None = None,
        variant: str | None = None,
        limit: int = 100,
    ) -> builtins.list[ProfileRecord]:
        if limit < 1 or limit > 10_000:
            raise AerError(
                "INVALID_ARGUMENT",
                "Profile list limit must be between 1 and 10000.",
                operation="profile.list",
                target=str(limit),
            )
        clauses: builtins.list[str] = []
        arguments: builtins.list[object] = []
        if task is not None:
            clauses.append("task = ?")
            arguments.append(task)
        if variant is not None:
            clauses.append("variant = ?")
            arguments.append(variant)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.settings.connect() as connection:
            rows = connection.execute(
                f"SELECT {', '.join(PROFILE_FIELDS)} FROM aer_profiles{where} "
                "ORDER BY timestamp DESC, rowid DESC LIMIT ?",
                (*arguments, limit),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def report(self, *, task: str | None = None) -> ProfileReport:
        records = self._all(task=task)
        variants = {
            variant: self._aggregate([record for record in records if record.variant == variant])
            for variant in sorted({record.variant for record in records})
        }
        return ProfileReport(
            generated_at=_timestamp(),
            task=task,
            overall=self._aggregate(records),
            variants=variants,
        )

    def compare(self, task: str) -> ProfileComparison:
        clean_task = self._label(task, name="task")
        report = self.report(task=clean_task)
        complete = {
            name: metrics.tokens_per_success
            for name, metrics in report.variants.items()
            if metrics.tokens_per_success_complete and metrics.tokens_per_success is not None
        }
        best = min(complete, key=complete.__getitem__) if complete else None
        differences: dict[str, dict[str, float]] = {}
        if best is not None:
            baseline = complete[best]
            assert baseline is not None
            for name, value in complete.items():
                assert value is not None
                absolute = value - baseline
                differences[name] = {
                    "tokens_per_success": value,
                    "absolute_more_than_lowest": absolute,
                    "percent_more_than_lowest": 0.0 if baseline == 0 else absolute / baseline * 100,
                }
        return ProfileComparison(
            task=clean_task,
            variants=report.variants,
            lowest_tokens_per_success_variant=best,
            differences_from_lowest=differences,
        )

    def export(self, output: str | Path) -> int:
        requested_destination = Path(output).expanduser()
        if requested_destination.is_symlink():
            raise AerError(
                "INVALID_ARGUMENT",
                "Profile export output cannot be a symbolic link.",
                operation="profile.export",
                target=str(output),
            )
        destination = requested_destination.resolve(strict=False)
        records = self._all()
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(PROFILE_FIELDS)
        for record in records:
            writer.writerow(
                int(bool(value)) if field == "success" else value
                for field, value in self._items(record)
            )
        atomic_write_text(destination, stream.getvalue())
        return len(records)

    def _initialize_database(self) -> None:
        with self.settings.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS aer_profiles (
                    task TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    model TEXT,
                    model_calls INTEGER CHECK(model_calls >= 0),
                    tool_calls INTEGER CHECK(tool_calls >= 0),
                    input_tokens INTEGER CHECK(input_tokens >= 0),
                    cached_input_tokens INTEGER CHECK(cached_input_tokens >= 0),
                    output_tokens INTEGER CHECK(output_tokens >= 0),
                    reasoning_tokens INTEGER CHECK(reasoning_tokens >= 0),
                    tool_schema_tokens INTEGER CHECK(tool_schema_tokens >= 0),
                    tool_result_tokens INTEGER CHECK(tool_result_tokens >= 0),
                    retries INTEGER CHECK(retries >= 0),
                    duration_ms INTEGER CHECK(duration_ms >= 0),
                    success INTEGER NOT NULL CHECK(success IN (0, 1)),
                    human_edits INTEGER CHECK(human_edits >= 0),
                    notes TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS aer_profiles_task_variant
                ON aer_profiles(task, variant, timestamp)
                """
            )

    def _all(self, *, task: str | None = None) -> builtins.list[ProfileRecord]:
        if task is None:
            where = ""
            arguments: tuple[object, ...] = ()
        else:
            where = " WHERE task = ?"
            arguments = (task,)
        with self.settings.connect() as connection:
            rows = connection.execute(
                f"SELECT {', '.join(PROFILE_FIELDS)} FROM aer_profiles{where} "
                "ORDER BY timestamp ASC, rowid ASC",
                arguments,
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _aggregate(records: builtins.list[ProfileRecord]) -> AggregateMetrics:
        record_count = len(records)
        successes = [record for record in records if record.success]
        success_count = len(successes)
        field_totals = {
            field: sum(
                int(value) for record in records if (value := getattr(record, field)) is not None
            )
            for field in NUMERIC_FIELDS
        }
        known_records = {
            field: sum(int(getattr(record, field) is not None) for record in records)
            for field in NUMERIC_FIELDS
        }
        missing_records = {field: record_count - known_records[field] for field in NUMERIC_FIELDS}
        reported_total = sum(record.reported_total_tokens for record in records)
        success_tokens_complete = all(not record.missing_token_fields for record in records)

        def per_success(field: str) -> tuple[float | None, bool]:
            if not successes:
                return None, False
            complete = all(getattr(record, field) is not None for record in records)
            total = sum(
                int(value) for record in records if (value := getattr(record, field)) is not None
            )
            return total / success_count, complete

        model_calls_per_success, model_calls_complete = per_success("model_calls")
        tool_calls_per_success, tool_calls_complete = per_success("tool_calls")

        def average(field: str) -> float | None:
            count = known_records[field]
            return None if count == 0 else field_totals[field] / count

        return AggregateMetrics(
            records=record_count,
            successes=success_count,
            failures=record_count - success_count,
            success_rate=None if record_count == 0 else success_count / record_count,
            reported_total_tokens=reported_total,
            tokens_per_success=(None if success_count == 0 else reported_total / success_count),
            tokens_per_success_complete=bool(successes) and success_tokens_complete,
            model_calls_per_success=model_calls_per_success,
            model_calls_per_success_complete=model_calls_complete,
            tool_calls_per_success=tool_calls_per_success,
            tool_calls_per_success_complete=tool_calls_complete,
            average_retries=average("retries"),
            average_duration_ms=average("duration_ms"),
            total_duration_ms=field_totals["duration_ms"],
            average_human_edits=average("human_edits"),
            field_totals=field_totals,
            known_records=known_records,
            missing_records=missing_records,
        )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ProfileRecord:
        return ProfileRecord(
            task=str(row["task"]),
            variant=str(row["variant"]),
            timestamp=str(row["timestamp"]),
            model=None if row["model"] is None else str(row["model"]),
            model_calls=ProfileStore._optional_int(row["model_calls"]),
            tool_calls=ProfileStore._optional_int(row["tool_calls"]),
            input_tokens=ProfileStore._optional_int(row["input_tokens"]),
            cached_input_tokens=ProfileStore._optional_int(row["cached_input_tokens"]),
            output_tokens=ProfileStore._optional_int(row["output_tokens"]),
            reasoning_tokens=ProfileStore._optional_int(row["reasoning_tokens"]),
            tool_schema_tokens=ProfileStore._optional_int(row["tool_schema_tokens"]),
            tool_result_tokens=ProfileStore._optional_int(row["tool_result_tokens"]),
            retries=ProfileStore._optional_int(row["retries"]),
            duration_ms=ProfileStore._optional_int(row["duration_ms"]),
            success=bool(row["success"]),
            human_edits=ProfileStore._optional_int(row["human_edits"]),
            notes=None if row["notes"] is None else str(row["notes"]),
        )

    @staticmethod
    def _items(record: ProfileRecord) -> builtins.list[tuple[str, object]]:
        return [(field, getattr(record, field)) for field in PROFILE_FIELDS]

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        raise AerError(
            "CORRUPT_FILE",
            "Profile database contains a non-integer numeric field.",
            operation="profile.report",
        )

    @staticmethod
    def _label(value: str, *, name: str) -> str:
        clean = value.strip()
        if not clean or len(clean.encode("utf-8")) > 256:
            raise AerError(
                "INVALID_ARGUMENT",
                f"Profile {name} must be a non-empty value of at most 256 bytes.",
                operation="profile.record",
                target=name,
            )
        return clean

    @staticmethod
    def _optional_text(value: str, *, name: str, limit: int) -> str:
        if len(value.encode("utf-8")) > limit:
            raise AerError(
                "LIMIT_EXCEEDED",
                f"Profile {name} exceeds the {limit}-byte limit.",
                operation="profile.record",
                target=name,
            )
        return value
