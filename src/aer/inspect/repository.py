"""Git-aware, ignore-aware repository inspection."""

from __future__ import annotations

import base64
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import threading
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import pathspec

from aer.errors import AerError
from aer.inspect.common import (
    RawSink,
    compact_line,
    is_sensitive_path,
    line_matches,
    preserve_overflow,
    read_text,
    safe_regex,
)

_DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "vendor",
}
_DEFAULT_EXCLUDED_SUFFIXES = {".class", ".o", ".pyc", ".so"}
_MAX_REPOSITORY_FILES = 100_000
_MAX_SEARCH_FILE_BYTES = 4 * 1024 * 1024
_MAX_REPOSITORY_MATCHES = 10_000
_MAX_MATCH_RESULT_BYTES = 64 * 1024 * 1024
_RG_ARG_BYTES = 64 * 1024
_RG_BATCH_FILES = 256
_RG_STDERR_BYTES = 8 * 1024
_RG_TIMEOUT_SECONDS = 30.0


class _RipgrepUnavailable(Exception):
    pass


@dataclass(slots=True)
class _MatchLimitReached(Exception):
    collector: _MatchCollector
    observed_at_least: int
    reason: str


@dataclass(slots=True)
class _MatchCollector:
    matches: list[dict[str, Any]] = field(default_factory=list)
    file_counts: Counter[str] = field(default_factory=Counter)
    encoded_bytes: int = 2

    def add(self, match: dict[str, Any]) -> None:
        observed = len(self.matches) + 1
        if observed > _MAX_REPOSITORY_MATCHES:
            raise _MatchLimitReached(self, observed, "match_count")
        encoded_size = len(
            json.dumps(
                match,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if self.encoded_bytes + encoded_size + 1 > _MAX_MATCH_RESULT_BYTES:
            raise _MatchLimitReached(self, observed, "encoded_bytes")
        self.matches.append(match)
        self.file_counts[str(match["file"])] += 1
        self.encoded_bytes += encoded_size + 1


@dataclass(slots=True)
class _SearchResult:
    collector: _MatchCollector
    engine: str
    skipped_binary: int = 0


def inspect_repository(
    root: Path,
    *,
    outline: bool,
    query: str | None,
    glob: str | None,
    changed: bool,
    regex: bool,
    case_sensitive: bool,
    context: int,
    max_items: int,
    raw_sink: RawSink | None,
    full: bool = False,
) -> dict[str, Any]:
    git_root = _git_root(root)
    if changed and git_root is None:
        raise AerError(
            "INVALID_ARGUMENT",
            "--changed requires a Git repository.",
            operation="inspect",
            target=str(root),
            suggested_action="Run without --changed or initialize a Git repository.",
        )
    files = _repository_files(root, git_root=git_root, changed=changed)
    ignore_spec = _load_aer_ignore(root, git_root)
    filtered: list[Path] = []
    for path in files:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if _excluded(relative, path, ignore_spec=ignore_spec):
            continue
        if glob is not None and not fnmatch.fnmatch(relative, glob):
            continue
        filtered.append(path)
        if len(filtered) > _MAX_REPOSITORY_FILES:
            raise AerError(
                "LIMIT_EXCEEDED",
                "Repository file count exceeds the inspection limit.",
                operation="inspect",
                target=str(root),
                details={"limit": _MAX_REPOSITORY_FILES},
            )

    sizes = {path: path.stat().st_size for path in filtered if path.is_file()}
    extensions = Counter(path.suffix.casefold() or "<none>" for path in filtered)
    result: dict[str, Any] = {
        "type": "repository",
        "git": git_root is not None,
        "file_count": len(filtered),
        "bytes": sum(sizes.values()),
        "extensions": dict(sorted(extensions.items())),
    }
    if changed:
        result["changed_only"] = True

    relative_files = [path.relative_to(root).as_posix() for path in filtered]
    if outline or query is None:
        result["outline"] = relative_files[:max_items]
        if len(relative_files) > max_items:
            result["truncated"] = True
            result["raw_ref"] = preserve_overflow(
                relative_files,
                raw_sink=raw_sink,
                name=f"{root.name or 'repository'}.files.json",
            )

    if query is not None:
        if not query:
            raise AerError(
                "INVALID_ARGUMENT",
                "Repository query must not be empty.",
                operation="inspect",
                target=str(root),
            )
        compiled = safe_regex(query, case_sensitive=case_sensitive) if regex else None
        skipped_large = 0
        search_files: list[Path] = []
        for path in filtered:
            if sizes.get(path, 0) > _MAX_SEARCH_FILE_BYTES:
                skipped_large += 1
                continue
            search_files.append(path)
        try:
            search = _search_repository(
                root,
                search_files,
                query=query,
                regex=compiled,
                case_sensitive=case_sensitive,
                context=context,
            )
        except _MatchLimitReached as exc:
            partial_ref = preserve_overflow(
                exc.collector.matches,
                raw_sink=raw_sink,
                name=f"{root.name or 'repository'}.matches.json",
            )
            raise AerError(
                "LIMIT_EXCEEDED",
                "Repository match count or encoded result size exceeds the search limit.",
                operation="inspect",
                target=str(root),
                details={
                    "match_limit": _MAX_REPOSITORY_MATCHES,
                    "encoded_bytes_limit": _MAX_MATCH_RESULT_BYTES,
                    "observed_at_least": exc.observed_at_least,
                    "reason": exc.reason,
                    "partial_results": len(exc.collector.matches),
                },
                suggested_action="Narrow the query or glob and retry.",
                raw_ref=partial_ref,
            ) from None
        exact_matches = search.collector.matches
        matches = exact_matches if full else [_compact_match_record(item) for item in exact_matches]
        text_truncated = any(_record_has_truncated_text(item) for item in matches)
        result.update(
            {
                "query": query,
                "match_count": len(matches),
                "file_match_counts": dict(sorted(search.collector.file_counts.items())),
                "matches": matches[:max_items],
                "search_engine": search.engine,
                "skipped_binary_or_undecodable": search.skipped_binary,
                "skipped_large": skipped_large,
            }
        )
        if len(matches) > max_items or text_truncated:
            result["truncated"] = True
            result["raw_ref"] = preserve_overflow(
                exact_matches,
                raw_sink=raw_sink,
                name=f"{root.name or 'repository'}.matches.json",
            )
    return result


@dataclass(slots=True)
class _LineCache:
    path: Path | None = None
    lines: list[str] = field(default_factory=list)
    failed: set[Path] = field(default_factory=set)

    def get(self, path: Path) -> list[str] | None:
        if path in self.failed:
            return None
        if path == self.path:
            return self.lines
        try:
            text, _, _ = read_text(path)
        except AerError as exc:
            if exc.code not in {"UNSUPPORTED_FORMAT", "CORRUPT_FILE", "LIMIT_EXCEEDED"}:
                raise
            self.failed.add(path)
            return None
        self.path = path
        self.lines = text.splitlines()
        return self.lines


def _search_repository(
    root: Path,
    files: list[Path],
    *,
    query: str,
    regex: re.Pattern[str] | None,
    case_sensitive: bool,
    context: int,
) -> _SearchResult:
    executable = shutil.which("rg")
    if executable is not None:
        try:
            return _search_with_ripgrep(
                root,
                files,
                executable=executable,
                query=query,
                regex=regex,
                case_sensitive=case_sensitive,
                context=context,
            )
        except _RipgrepUnavailable:
            pass
    return _search_with_python(
        root,
        files,
        query=query,
        regex=regex,
        case_sensitive=case_sensitive,
        context=context,
    )


def _search_with_python(
    root: Path,
    files: list[Path],
    *,
    query: str,
    regex: re.Pattern[str] | None,
    case_sensitive: bool,
    context: int,
) -> _SearchResult:
    collector = _MatchCollector()
    skipped = 0
    for path in files:
        try:
            text, _, _ = read_text(path)
        except AerError as exc:
            if exc.code in {"UNSUPPORTED_FORMAT", "CORRUPT_FILE", "LIMIT_EXCEEDED"}:
                skipped += 1
                continue
            raise
        lines = text.splitlines()
        relative = path.relative_to(root).as_posix()
        for index, line in enumerate(lines):
            if line_matches(
                line,
                query,
                regex=regex,
                case_sensitive=case_sensitive,
            ):
                collector.add(_match_record(relative, lines, index + 1, context=context))
    return _SearchResult(collector=collector, engine="python", skipped_binary=skipped)


def _search_with_ripgrep(
    root: Path,
    files: list[Path],
    *,
    executable: str,
    query: str,
    regex: re.Pattern[str] | None,
    case_sensitive: bool,
    context: int,
) -> _SearchResult:
    collector = _MatchCollector()
    cache = _LineCache()
    searchable: list[Path] = []
    skipped_paths: set[Path] = set()
    for path in files:
        if _probably_binary(path):
            skipped_paths.add(path)
        else:
            searchable.append(path)
    relative_paths = [(path.relative_to(root).as_posix(), path) for path in searchable]
    by_relative = dict(relative_paths)
    for batch in _path_batches([relative for relative, _ in relative_paths]):
        command = [
            executable,
            "--no-config",
            "--json",
            "--line-number",
            "--color=never",
            "--no-messages",
            "--case-sensitive" if case_sensitive else "--ignore-case",
        ]
        if regex is None:
            command.append("--fixed-strings")
        command.extend(["-e", query, "--", *batch])
        try:
            process = subprocess.Popen(
                command,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise _RipgrepUnavailable from exc

        assert process.stdout is not None
        assert process.stderr is not None
        stderr = bytearray()
        stderr_reader = threading.Thread(
            target=_drain_bounded,
            args=(process.stderr, stderr, _RG_STDERR_BYTES),
            name="aer-rg-stderr",
            daemon=True,
        )
        stderr_reader.start()
        timed_out = threading.Event()

        timer = threading.Timer(
            _RG_TIMEOUT_SECONDS,
            _expire_process,
            args=(process, timed_out),
        )
        timer.daemon = True
        timer.start()
        try:
            for raw_event in process.stdout:
                try:
                    event = json.loads(raw_event)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise _RipgrepUnavailable from exc
                if not isinstance(event, dict) or event.get("type") != "match":
                    continue
                data = event.get("data")
                if not isinstance(data, dict):
                    raise _RipgrepUnavailable
                relative = _rg_text(data.get("path"))
                line_number = data.get("line_number")
                if relative is None or not isinstance(line_number, int):
                    raise _RipgrepUnavailable
                normalized = Path(relative).as_posix()
                if normalized.startswith("./"):
                    normalized = normalized[2:]
                matched_path = by_relative.get(normalized)
                if matched_path is None:
                    raise _RipgrepUnavailable
                lines = cache.get(matched_path)
                if lines is None:
                    skipped_paths.add(matched_path)
                    continue
                if line_number < 1 or line_number > len(lines):
                    raise _RipgrepUnavailable
                collector.add(_match_record(normalized, lines, line_number, context=context))
        except BaseException:
            _kill_process(process)
            process.wait()
            timer.cancel()
            timer.join()
            stderr_reader.join(timeout=1)
            raise
        finally:
            process.stdout.close()
        return_code = process.wait()
        timer.cancel()
        timer.join()
        stderr_reader.join(timeout=1)
        if timed_out.is_set():
            raise AerError(
                "COMMAND_TIMEOUT",
                "Repository search exceeded the ripgrep timeout.",
                operation="inspect",
                target=str(root),
                details={"timeout_seconds": _RG_TIMEOUT_SECONDS},
                suggested_action="Narrow the query or glob and retry.",
            )
        if stderr_reader.is_alive():
            process.stderr.close()
            raise _RipgrepUnavailable
        if return_code not in {0, 1}:
            raise _RipgrepUnavailable
    return _SearchResult(
        collector=collector,
        engine="ripgrep",
        skipped_binary=len(skipped_paths | cache.failed),
    )


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    """Stop one ripgrep process and its POSIX process group if it is still alive."""

    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        return
    except OSError:
        with suppress(OSError):
            process.kill()


def _expire_process(process: subprocess.Popen[bytes], timed_out: threading.Event) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
    elif process.poll() is not None:
        return
    timed_out.set()
    _kill_process(process)


def _path_batches(paths: list[str]) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0
    for path in paths:
        encoded_size = len(os.fsencode(path)) + 1
        if current and (
            len(current) >= _RG_BATCH_FILES or current_bytes + encoded_size > _RG_ARG_BYTES
        ):
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(path)
        current_bytes += encoded_size
    if current:
        batches.append(current)
    return batches


def _drain_bounded(stream: BinaryIO, destination: bytearray, limit: int) -> None:
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            remaining = limit - len(destination)
            if remaining > 0:
                destination.extend(chunk[:remaining])
    finally:
        stream.close()


def _rg_text(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    text = value.get("text")
    if isinstance(text, str):
        return text
    encoded = value.get("bytes")
    if not isinstance(encoded, str):
        return None
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8", errors="surrogateescape")
    except (ValueError, UnicodeDecodeError):
        return None


def _probably_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            sample = handle.read(8192)
    except OSError:
        return True
    return b"\x00" in sample and not sample.startswith((b"\xff\xfe", b"\xfe\xff"))


def _match_record(
    relative: str,
    lines: list[str],
    line_number: int,
    *,
    context: int,
) -> dict[str, Any]:
    index = line_number - 1
    before_start = max(0, index - context)
    after_end = min(len(lines), index + context + 1)
    return {
        "file": relative,
        "line": line_number,
        "text": lines[index],
        "before": [
            {"line": number + 1, "text": lines[number]} for number in range(before_start, index)
        ],
        "after": [
            {"line": number + 1, "text": lines[number]} for number in range(index + 1, after_end)
        ],
    }


def _compact_match_record(record: dict[str, Any]) -> dict[str, Any]:
    preview = {**record, "text": compact_line(str(record["text"]))}
    if preview["text"] != str(record["text"]).rstrip("\r\n"):
        preview["text_truncated"] = True
    preview["before"] = [_compact_context_record(item) for item in record["before"]]
    preview["after"] = [_compact_context_record(item) for item in record["after"]]
    return preview


def _compact_context_record(record: dict[str, Any]) -> dict[str, Any]:
    exact = str(record["text"])
    preview = compact_line(exact)
    result = {**record, "text": preview}
    if preview != exact.rstrip("\r\n"):
        result["text_truncated"] = True
    return result


def _record_has_truncated_text(record: dict[str, Any]) -> bool:
    return bool(
        record.get("text_truncated")
        or any(item.get("text_truncated") for item in record.get("before", []))
        or any(item.get("text_truncated") for item in record.get("after", []))
    )


def _git_root(root: Path) -> Path | None:
    completed = _run_git(root, ["rev-parse", "--show-toplevel"])
    if completed.returncode != 0:
        return None
    value = completed.stdout.decode("utf-8", errors="replace").strip()
    return Path(value).resolve() if value else None


def _repository_files(root: Path, *, git_root: Path | None, changed: bool) -> list[Path]:
    if git_root is None:
        return _walk_files(root)
    names = _git_changed(git_root) if changed else _git_names(git_root)
    paths: list[Path] = []
    for name in names:
        candidate = git_root / name
        if candidate.is_symlink():
            continue
        path = candidate.resolve(strict=False)
        if path.is_file():
            paths.append(path)
    return paths


def _git_names(git_root: Path) -> list[str]:
    tracked = _nul_names(_run_git(git_root, ["ls-files", "-z"]).stdout)
    untracked = _nul_names(
        _run_git(git_root, ["ls-files", "-z", "--others", "--exclude-standard"]).stdout
    )
    return sorted(dict.fromkeys(tracked)) + sorted(set(untracked) - set(tracked))


def _git_changed(git_root: Path) -> list[str]:
    commands = (
        ["diff", "--name-only", "-z"],
        ["diff", "--cached", "--name-only", "-z"],
        ["ls-files", "-z", "--others", "--exclude-standard"],
    )
    names: set[str] = set()
    for command in commands:
        names.update(_nul_names(_run_git(git_root, command).stdout))
    return sorted(names)


def _run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=1, stdout=b"", stderr=b""
        )


def _nul_names(value: bytes) -> list[str]:
    return [name.decode("utf-8", errors="surrogateescape") for name in value.split(b"\0") if name]


def _walk_files(root: Path) -> list[Path]:
    result: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        children = sorted(directory.iterdir(), key=lambda value: value.name, reverse=True)
        for child in children:
            if child.is_symlink():
                continue
            if child.is_dir():
                if child.name not in _DEFAULT_EXCLUDED_DIRS:
                    stack.append(child)
            elif child.is_file():
                result.append(child)
    return sorted(result)


def _load_aer_ignore(root: Path, git_root: Path | None) -> pathspec.GitIgnoreSpec | None:
    patterns: list[str] = []
    candidates = [root / ".aerignore"]
    if git_root is None:
        candidates.append(root / ".gitignore")
    elif git_root != root:
        candidates.append(git_root / ".aerignore")
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            patterns.extend(candidate.read_text(encoding="utf-8", errors="replace").splitlines())
    return pathspec.GitIgnoreSpec.from_lines(patterns) if patterns else None


def _excluded(relative: str, path: Path, *, ignore_spec: pathspec.GitIgnoreSpec | None) -> bool:
    parts = Path(relative).parts
    if any(part in _DEFAULT_EXCLUDED_DIRS for part in parts):
        return True
    if path.suffix.casefold() in _DEFAULT_EXCLUDED_SUFFIXES or is_sensitive_path(path):
        return True
    return ignore_spec.match_file(relative) if ignore_spec is not None else False
