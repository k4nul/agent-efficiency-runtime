"""Small content-hash cache built on the disposable object namespace."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from filelock import FileLock

from aer.config import Settings
from aer.errors import AerError
from aer.hashing import normalized_hash
from aer.store import ObjectRecord, ObjectStore

_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_METADATA_BYTES = 64 * 1024


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None = None) -> str:
    moment = value or _utc_now()
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AerError(
            "CORRUPT_FILE",
            "Cache metadata contains an invalid timestamp.",
            operation="cache.metadata",
            target=value,
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AerError(
            "CORRUPT_FILE",
            "Cache timestamp does not include a timezone.",
            operation="cache.metadata",
            target=value,
        )
    return parsed.astimezone(UTC)


def make_cache_key(
    capability: str,
    version: str,
    input_hash: str,
    *,
    spec_hash: str | None = None,
    configuration: Mapping[str, object] | None = None,
    dependency_versions: Mapping[str, str] | None = None,
) -> str:
    """Build a stable key from correctness-relevant cache inputs."""

    if not capability.strip() or not version.strip() or not input_hash.strip():
        raise AerError(
            "INVALID_ARGUMENT",
            "capability, version, and input_hash are required for a cache key.",
            operation="cache.key",
        )
    return normalized_hash(
        {
            "capability": capability,
            "version": version,
            "input_hash": input_hash,
            "spec_hash": spec_hash,
            "configuration": dict(configuration or {}),
            "dependency_versions": dict(dependency_versions or {}),
        }
    )


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A cache key mapped to a content-addressed object."""

    key: str
    ref: str
    size: int
    created_at: str
    accessed_at: str
    expires_at: str | None
    metadata: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "ref": self.ref,
            "size": self.size,
            "created_at": self.created_at,
            "accessed_at": self.accessed_at,
            "expires_at": self.expires_at,
            "metadata": self.metadata,
        }


class ContentHashCache:
    """Map normalized operation keys to deduplicated disposable content."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.load()
        self.store = ObjectStore(self.settings, namespace="cache")
        self._index_lock = FileLock(
            self.settings.cache_dir / ".index.lock",
            timeout=30,
            mode=0o600,
            preserve_lock_file=True,
            fallback_to_soft=False,
        )
        self._initialize_database()

    @staticmethod
    def key_for(
        capability: str,
        version: str,
        input_hash: str,
        *,
        spec_hash: str | None = None,
        configuration: Mapping[str, object] | None = None,
        dependency_versions: Mapping[str, str] | None = None,
    ) -> str:
        return make_cache_key(
            capability,
            version,
            input_hash,
            spec_hash=spec_hash,
            configuration=configuration,
            dependency_versions=dependency_versions,
        )

    def put(
        self,
        key: str,
        data: bytes,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        metadata: Mapping[str, object] | None = None,
        ttl: timedelta | None = None,
    ) -> CacheEntry:
        """Store bytes and atomically publish their cache-key mapping."""

        safe_key = self._validate_key(key, operation="cache.put")
        metadata_json = self._serialize_metadata(metadata, operation="cache.put")
        expires_at = self._expiry(ttl, operation="cache.put")
        with self._index_lock:
            record = self.store.put_bytes(
                data,
                filename=filename,
                mime_type=mime_type,
                source={"cache_key": safe_key},
            )
            return self._map_record(
                safe_key,
                record,
                metadata_json=metadata_json,
                expires_at=expires_at,
            )

    def put_file(
        self,
        key: str,
        path: str | Path,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        metadata: Mapping[str, object] | None = None,
        ttl: timedelta | None = None,
    ) -> CacheEntry:
        """Store a regular file and publish its cache-key mapping."""

        safe_key = self._validate_key(key, operation="cache.put")
        metadata_json = self._serialize_metadata(metadata, operation="cache.put")
        expires_at = self._expiry(ttl, operation="cache.put")
        with self._index_lock:
            record = self.store.put_file(
                path,
                filename=filename,
                mime_type=mime_type,
                source={"cache_key": safe_key},
            )
            return self._map_record(
                safe_key,
                record,
                metadata_json=metadata_json,
                expires_at=expires_at,
            )

    def get(self, key: str) -> CacheEntry | None:
        """Return a live mapping, treating absent/expired objects as cache misses."""

        safe_key = self._validate_key(key, operation="cache.get")
        with self._index_lock:
            with self.settings.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM aer_cache_entries WHERE cache_key = ?", (safe_key,)
                ).fetchone()
            if row is None:
                return None
            entry = self._row_to_entry(row)
            if entry.expires_at is not None and _parse_timestamp(entry.expires_at) <= _utc_now():
                self._delete_locked(safe_key)
                return None
            try:
                record = self.store.stat(entry.ref)
            except AerError as exc:
                if exc.code == "NOT_FOUND":
                    self._delete_mapping_only(safe_key)
                    return None
                raise
            now = _timestamp()
            with self.settings.connect() as connection:
                connection.execute(
                    "UPDATE aer_cache_entries SET accessed_at = ? WHERE cache_key = ?",
                    (now, safe_key),
                )
            return CacheEntry(
                key=entry.key,
                ref=entry.ref,
                size=record.size,
                created_at=entry.created_at,
                accessed_at=now,
                expires_at=entry.expires_at,
                metadata=entry.metadata,
            )

    def get_bytes(self, key: str) -> bytes | None:
        """Return cached bytes or None; corrupt content remains an explicit error."""

        entry = self.get(key)
        if entry is None:
            return None
        try:
            return self.store.get_bytes(entry.ref)
        except AerError as exc:
            if exc.code == "NOT_FOUND":
                self._delete_mapping_only(entry.key)
                return None
            raise

    def delete(self, key: str) -> bool:
        """Delete a mapping and remove content when no other key references it."""

        safe_key = self._validate_key(key, operation="cache.delete")
        with self._index_lock:
            return self._delete_locked(safe_key)

    def clear(self) -> int:
        """Remove all cache mappings and cache-namespace objects."""

        with self._index_lock:
            with self.settings.connect() as connection:
                count_row = connection.execute("SELECT COUNT(*) FROM aer_cache_entries").fetchone()
                connection.execute("DELETE FROM aer_cache_entries")
            while objects := self.store.list(limit=1000):
                for record in objects:
                    self.store.delete(record.ref, force=True)
            return 0 if count_row is None else int(count_row[0])

    def purge_expired(self) -> int:
        """Remove expired mappings using the same safe deletion path."""

        now = _timestamp()
        with self._index_lock:
            with self.settings.connect() as connection:
                keys = [
                    str(row["cache_key"])
                    for row in connection.execute(
                        """
                        SELECT cache_key FROM aer_cache_entries
                        WHERE expires_at IS NOT NULL AND expires_at <= ?
                        """,
                        (now,),
                    ).fetchall()
                ]
            return sum(int(self._delete_locked(key)) for key in keys)

    def list(self, *, limit: int = 20, offset: int = 0) -> list[CacheEntry]:
        """List live cache mappings newest-first."""

        if limit < 1 or limit > 1000 or offset < 0:
            raise AerError(
                "INVALID_ARGUMENT",
                "limit must be 1..1000 and offset cannot be negative.",
                operation="cache.list",
            )
        self.purge_expired()
        with self.settings.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM aer_cache_entries
                ORDER BY created_at DESC, cache_key ASC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def _initialize_database(self) -> None:
        with self.settings.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS aer_cache_entries (
                    cache_key TEXT PRIMARY KEY,
                    ref TEXT NOT NULL,
                    size INTEGER NOT NULL CHECK(size >= 0),
                    created_at TEXT NOT NULL,
                    accessed_at TEXT NOT NULL,
                    expires_at TEXT,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS aer_cache_expiry
                ON aer_cache_entries(expires_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS aer_cache_ref
                ON aer_cache_entries(ref)
                """
            )

    def _map_record(
        self,
        key: str,
        record: ObjectRecord,
        *,
        metadata_json: str,
        expires_at: str | None,
    ) -> CacheEntry:
        now = _timestamp()
        with self.settings.connect() as connection:
            previous = connection.execute(
                "SELECT ref FROM aer_cache_entries WHERE cache_key = ?", (key,)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO aer_cache_entries (
                    cache_key, ref, size, created_at, accessed_at,
                    expires_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    ref = excluded.ref,
                    size = excluded.size,
                    created_at = excluded.created_at,
                    accessed_at = excluded.accessed_at,
                    expires_at = excluded.expires_at,
                    metadata_json = excluded.metadata_json
                """,
                (key, record.ref, record.size, now, now, expires_at, metadata_json),
            )
            row = connection.execute(
                "SELECT * FROM aer_cache_entries WHERE cache_key = ?", (key,)
            ).fetchone()
            old_ref = None if previous is None else str(previous["ref"])
            if old_ref is not None and old_ref != record.ref:
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM aer_cache_entries WHERE ref = ?", (old_ref,)
                ).fetchone()
            else:
                remaining = None
        if old_ref is not None and remaining is not None and int(remaining[0]) == 0:
            self.store.delete(old_ref, force=True)
        if row is None:
            raise AerError(
                "INTERNAL_ERROR",
                "Cache mapping could not be recorded.",
                operation="cache.put",
                target=key,
            )
        return self._row_to_entry(row)

    def _delete_mapping_only(self, key: str) -> None:
        with self.settings.connect() as connection:
            connection.execute("DELETE FROM aer_cache_entries WHERE cache_key = ?", (key,))

    def _delete_locked(self, key: str) -> bool:
        with self.settings.connect() as connection:
            row = connection.execute(
                "SELECT ref FROM aer_cache_entries WHERE cache_key = ?", (key,)
            ).fetchone()
            if row is None:
                return False
            ref = str(row["ref"])
            connection.execute("DELETE FROM aer_cache_entries WHERE cache_key = ?", (key,))
            remaining = connection.execute(
                "SELECT COUNT(*) FROM aer_cache_entries WHERE ref = ?", (ref,)
            ).fetchone()
        if remaining is not None and int(remaining[0]) == 0:
            self.store.delete(ref, force=True)
        return True

    @staticmethod
    def _validate_key(key: str, *, operation: str) -> str:
        if not _KEY_PATTERN.fullmatch(key):
            raise AerError(
                "INVALID_ARGUMENT",
                "Cache key must be exactly 64 lowercase hexadecimal characters.",
                operation=operation,
                target=key,
            )
        return key

    @staticmethod
    def _serialize_metadata(
        metadata: Mapping[str, object] | None,
        *,
        operation: str,
    ) -> str:
        try:
            encoded = json.dumps(
                dict(metadata or {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise AerError(
                "INVALID_ARGUMENT",
                "Cache metadata must contain JSON-serializable values.",
                operation=operation,
            ) from exc
        if len(encoded.encode("utf-8")) > _MAX_METADATA_BYTES:
            raise AerError(
                "LIMIT_EXCEEDED",
                "Cache metadata exceeds the 64 KiB limit.",
                operation=operation,
            )
        return encoded

    @staticmethod
    def _expiry(ttl: timedelta | None, *, operation: str) -> str | None:
        if ttl is None:
            return None
        if ttl.total_seconds() <= 0:
            raise AerError(
                "INVALID_ARGUMENT",
                "Cache TTL must be greater than zero.",
                operation=operation,
            )
        return _timestamp(_utc_now() + ttl)

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> CacheEntry:
        mapping = dict(row)
        try:
            loaded = json.loads(str(mapping["metadata_json"]))
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise AerError(
                "CORRUPT_FILE",
                "Cache entry contains invalid metadata JSON.",
                operation="cache.metadata",
            ) from exc
        if not isinstance(loaded, dict):
            raise AerError(
                "CORRUPT_FILE",
                "Cache entry metadata must be a JSON object.",
                operation="cache.metadata",
            )
        metadata: dict[str, object] = {str(key): value for key, value in loaded.items()}
        expires = mapping.get("expires_at")
        return CacheEntry(
            key=str(mapping["cache_key"]),
            ref=str(mapping["ref"]),
            size=int(mapping["size"]),
            created_at=str(mapping["created_at"]),
            accessed_at=str(mapping["accessed_at"]),
            expires_at=None if expires is None else str(expires),
            metadata=metadata,
        )
