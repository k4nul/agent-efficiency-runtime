"""Run argv commands without a shell and return a compact diagnostic result."""

from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import BinaryIO, Protocol

from aer.errors import AerError
from aer.limits import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    LOG_PREVIEW_BYTES,
    LOG_PREVIEW_LINES,
)
from aer.protocol import Metrics, success

_OUTPUT_LIMIT_BYTES = 256 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_FAILURE_CONTEXT_BYTES = 8 * 1024
_SUMMARY_BYTES = 1024
_WARNING_MESSAGE_BYTES = 256
_MAX_WARNING_KEYS = 4096
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_ERROR_RE = re.compile(
    r"(?:\berror\b|\bfailed\b|\bfailure\b|\bfatal\b|\bexception\b|"
    r"traceback|assertionerror|syntaxerror|typeerror|referenceerror|segmentation fault)",
    re.IGNORECASE,
)
_WARNING_RE = re.compile(r"\bwarn(?:ing)?\b", re.IGNORECASE)
_PROGRESS_RE = re.compile(
    r"^\s*(?:(?:[.oO=#>-]+\s*)?\d{1,3}%|\d+\s*/\s*\d+\s*(?:items?|files?|tests?)?)"
    r"(?:\s*\[[^]]*])?(?:\s+\d+(?:\.\d+)?\s*(?:[kMGT]?B|it)/s)?\s*$",
    re.IGNORECASE,
)
_SECRET_HINT_RE = re.compile(
    r"[:=]|-----|sk-|gh[pousr]_|github_pat_|(?:AKIA|ASIA)[0-9A-Z]{16}|"
    r"(?:aws[ _-]?(?:(?:access|secret)[ _-]?(?:access[ _-]?)?key(?:[ _-]?id)?|"
    r"(?:session|security)[ _-]?token|credential)|"
    r"x[ _-]?amz[ _-]?security[ _-]?token)|"
    r"\b(?:authorization|cookie|password|passwd|token|secret|credential)\b",
    re.IGNORECASE,
)


class StoredObject(Protocol):
    """The part of ObjectRecord needed by the runner."""

    @property
    def ref(self) -> str: ...


class ContentStore(Protocol):
    """Structural type implemented by :class:`aer.store.ObjectStore`."""

    def put_stream(
        self,
        stream: BinaryIO,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        source: Mapping[str, object] | None = None,
    ) -> StoredObject: ...


@dataclass(slots=True)
class WarningCount:
    message: str
    count: int


@dataclass(slots=True)
class CommandResult:
    """A bounded command result; the complete sanitized capture lives in ``raw_ref``."""

    exit_code: int
    duration_ms: int
    timed_out: bool
    summary: str | None
    failure_context: list[str] = field(default_factory=list)
    warnings: list[WarningCount] = field(default_factory=list)
    raw_ref: str | None = None
    bytes_captured: int = 0
    log_truncated: bool = False
    output_limit_exceeded: bool = False

    @property
    def ok(self) -> bool:
        return not self.timed_out and not self.output_limit_exceeded and self.exit_code == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "summary": self.summary,
            "failure_context": self.failure_context,
            "warnings": [asdict(item) for item in self.warnings],
            "raw_ref": self.raw_ref,
            "bytes_captured": self.bytes_captured,
            "log_truncated": self.log_truncated,
            "output_limit_exceeded": self.output_limit_exceeded,
        }


@dataclass(slots=True)
class _Capture:
    path: Path
    total_bytes: int = 0
    error: BaseException | None = None


@dataclass(slots=True)
class _CaptureBudget:
    limit: int
    total_bytes: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    exceeded: threading.Event = field(default_factory=threading.Event)
    failed: threading.Event = field(default_factory=threading.Event)

    def account(self, size: int) -> None:
        with self.lock:
            self.total_bytes += size
            if self.total_bytes > self.limit:
                self.exceeded.set()


@dataclass(slots=True)
class _PrivateKeyState:
    active: bool = False


class _Diagnostics:
    """Bounded diagnostics accumulated while a complete log is streamed to disk."""

    def __init__(self) -> None:
        self._index = 0
        self._error_count = 0
        self._pending_until = -1
        self._previous: deque[tuple[int, str]] = deque(maxlen=2)
        self._selected: dict[int, str] = {}
        self._tail: deque[str] = deque(maxlen=20)
        self._warnings: Counter[str] = Counter()

    def add(self, line: str) -> None:
        index = self._index
        self._index += 1
        context_line = _truncate_utf8(line, _FAILURE_CONTEXT_BYTES)
        self._tail.append(context_line)
        if index <= self._pending_until:
            self._selected[index] = context_line
        if _ERROR_RE.search(line) and self._error_count < 20:
            self._error_count += 1
            self._selected.update(self._previous)
            self._selected[index] = context_line
            self._pending_until = max(self._pending_until, index + 2)
        self._previous.append((index, context_line))

        if _WARNING_RE.search(line):
            message = _truncate_utf8(line.strip(), _WARNING_MESSAGE_BYTES)
            if message in self._warnings or len(self._warnings) < _MAX_WARNING_KEYS:
                self._warnings[message] += 1

    def failure_context(self) -> list[str]:
        if not self._selected:
            return _bounded_lines(list(self._tail))
        contextual: list[str] = []
        previous = -2
        for index, line in sorted(self._selected.items()):
            if index > previous + 1 and contextual:
                contextual.append("...")
            contextual.append(line)
            previous = index
        return _bounded_lines(contextual)

    def warning_counts(self) -> list[WarningCount]:
        return [
            WarningCount(message=message, count=count)
            for message, count in sorted(
                self._warnings.items(), key=lambda item: (-item[1], item[0])
            )[:20]
        ]


def redact_secrets(text: str) -> str:
    """Redact common credentials from previews and the persisted raw log."""

    # Most build output is plain prose or repeated progress payload. Avoid running
    # every credential regex across large lines that cannot contain a supported form.
    if _SECRET_HINT_RE.search(text) is None:
        return text
    redacted = re.sub(
        r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?"
        r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    json_secret_label = (
        r"authorization|api[ _-]?key|access[ _-]?token|auth[ _-]?token|password|"
        r"passwd|pwd|cookie|set-cookie|client[ _-]?secret|"
        r"aws[ _-]?secret[ _-]?access[ _-]?key|aws[ _-]?access[ _-]?key[ _-]?id|"
        r"aws[ _-]?(?:session|security)[ _-]?token|"
        r"aws[ _-]?(?:access|secret)[ _-]?key|aws[ _-]?credential|"
        r"database[ _-]?url|github[ _-]?token|openai[ _-]?token"
    )
    quoted_json_secret = re.compile(
        rf"(?i)([\"'](?:{json_secret_label})[\"']\s*:\s*)([\"'])[^\r\n]*?\2"
    )
    redacted = quoted_json_secret.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]{match.group(2)}",
        redacted,
    )
    redacted = re.sub(
        r"(?im)^([^\r\n]*?(?<![\"'])\b(?:cookie|set-cookie)\s*:\s*).*$",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?im)^([^\r\n]*?(?<![\"'])\bauthorization\s*[:=]\s*).*$",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
        r"password|passwd|pwd|cookie|set[_-]?cookie|client[_-]?secret|"
        r"secret[_-]?access[_-]?key|access[_-]?key[_-]?id|database[_-]?url|"
        r"(?:aws[_-]?)?(?:session|security)[_-]?token|"
        r"(?:aws[_-]?)?(?:access|secret)[_-]?key|aws[_-]?credential|"
        r"github[_-]?token|openai[_-]?token)\b\s*(?:[:=]\s*|\s+))"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,}|"
        r"github_pat_[A-Za-z0-9_]{20,}|(?:AKIA|ASIA)[0-9A-Z]{16})\b",
        "[REDACTED TOKEN]",
        redacted,
    )
    # Preserve the scheme and host while removing URL userinfo.
    redacted = re.sub(
        r"(?i)\b([a-z][a-z0-9+.-]*://)([^\s/@:]+):([^\s/@]+)@",
        r"\1[REDACTED]@",
        redacted,
    )
    return redacted


def _drain_stream(stream: BinaryIO, capture: _Capture, budget: _CaptureBudget) -> None:
    try:
        descriptor = os.open(
            capture.path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as destination:
            while True:
                chunk = stream.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                destination.write(chunk)
                capture.total_bytes += len(chunk)
                budget.account(len(chunk))
    except BaseException as exc:  # pragma: no cover - an OS-level pipe failure
        capture.error = exc
        budget.failed.set()
    finally:
        stream.close()


def _wait_for_process_group_exit(process_group: int, *, timeout: float = 1.0) -> None:
    if os.name != "posix":  # pragma: no cover - exercised on Windows CI
        return
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            return
        time.sleep(0.01)


def _terminate_process_tree(process: subprocess.Popen[bytes], *, force: bool = False) -> None:
    if os.name == "posix":
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    else:  # pragma: no cover - exercised on Windows CI
        if process.poll() is None:
            process.kill() if force else process.terminate()
    if force:
        if process.poll() is None:
            process.wait()
        _wait_for_process_group_exit(process.pid)
        return
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1.0)
    if os.name == "posix":
        with suppress(ProcessLookupError):
            # The direct child may exit while descendants keep inherited pipes open.
            # Reap the remaining process group before diagnostics consume the spool.
            os.killpg(process.pid, signal.SIGKILL)
    else:  # pragma: no cover - exercised on Windows CI
        if process.poll() is None:
            process.kill()
    if process.poll() is None:
        process.wait()
    _wait_for_process_group_exit(process.pid)


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    marker = "..."
    available = max_bytes - len(marker)
    return encoded[:available].decode("utf-8", errors="ignore") + marker


def _bounded_lines(lines: Sequence[str]) -> list[str]:
    bounded: list[str] = []
    used = 0
    for line in lines:
        if len(bounded) >= LOG_PREVIEW_LINES:
            break
        available = min(LOG_PREVIEW_BYTES, _FAILURE_CONTEXT_BYTES) - used
        if available <= 0:
            break
        encoded = line.encode("utf-8", errors="replace")
        if len(encoded) + 1 > available:
            fragment = encoded[: max(0, available - 1)].decode("utf-8", errors="ignore")
            if fragment:
                bounded.append(fragment)
            break
        bounded.append(line)
        used += len(encoded) + 1
    return bounded


_PRIVATE_KEY_BEGIN_RE = re.compile(r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----", re.IGNORECASE)
_PRIVATE_KEY_END_RE = re.compile(r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----", re.IGNORECASE)


def _raw_physical_line(raw_line: bytes) -> str:
    """Decode a captured line and remove terminal escape sequences only."""

    line = raw_line.decode("utf-8", errors="replace").removesuffix("\n")
    return _ANSI_OSC_RE.sub("", _ANSI_CSI_RE.sub("", line))


def _diagnostic_line(line: str) -> str | None:
    """Normalize one redacted line for the bounded model-facing diagnostics."""

    line = line.rsplit("\r", 1)[-1].rstrip()
    if not line or (_PROGRESS_RE.fullmatch(line) and not _ERROR_RE.search(line)):
        return None
    return line


def _redact_private_key_line(line: str, state: _PrivateKeyState) -> str | None:
    if state.active:
        end = _PRIVATE_KEY_END_RE.search(line)
        if end is None:
            return None
        state.active = False
        suffix = line[end.end() :]
        return redact_secrets(suffix) if suffix else None

    begin = _PRIVATE_KEY_BEGIN_RE.search(line)
    if begin is None:
        return redact_secrets(line)
    prefix = redact_secrets(line[: begin.start()])
    remainder = line[begin.end() :]
    end = _PRIVATE_KEY_END_RE.search(remainder)
    marker = "[REDACTED PRIVATE KEY]"
    if end is None:
        state.active = True
        return prefix + marker
    suffix = redact_secrets(remainder[end.end() :])
    return prefix + marker + suffix


def _stream_sanitized_section(
    source: Path,
    destination: BinaryIO,
    *,
    label: str,
    diagnostics: _Diagnostics,
) -> str | None:
    """Append a redacted stream while deriving compact diagnostics separately."""

    state = _PrivateKeyState()
    wrote_header = False
    last_line: str | None = None
    with source.open("rb") as handle:
        for raw_line in handle:
            line = _redact_private_key_line(_raw_physical_line(raw_line), state)
            if line is None:
                continue
            if not wrote_header:
                destination.write(f"[{label}]\n".encode())
                wrote_header = True
            destination.write(line.encode("utf-8", errors="replace") + b"\n")
            diagnostic = _diagnostic_line(line)
            if diagnostic is not None:
                diagnostics.add(diagnostic)
                last_line = _truncate_utf8(diagnostic, _SUMMARY_BYTES)
    return last_line


def _write_sanitized_log(
    stdout_path: Path,
    stderr_path: Path,
    destination_path: Path,
) -> tuple[str | None, str | None, _Diagnostics]:
    diagnostics = _Diagnostics()
    descriptor = os.open(
        destination_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as destination:
        stdout_last = _stream_sanitized_section(
            stdout_path,
            destination,
            label="stdout",
            diagnostics=diagnostics,
        )
        stderr_last = _stream_sanitized_section(
            stderr_path,
            destination,
            label="stderr",
            diagnostics=diagnostics,
        )
        if destination.tell() == 0:
            destination.write(b"\n")
    return stdout_last, stderr_last, diagnostics


def _wait_for_process(
    process: subprocess.Popen[bytes],
    budget: _CaptureBudget,
    *,
    started: float,
    timeout: float,
) -> tuple[bool, bool]:
    deadline = started + timeout
    timed_out = False
    output_limit_exceeded = False
    try:
        while process.poll() is None:
            if budget.failed.is_set():
                _terminate_process_tree(process, force=True)
                break
            if budget.exceeded.is_set():
                output_limit_exceeded = True
                _terminate_process_tree(process, force=True)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_process_tree(process)
                break
            try:
                process.wait(timeout=min(0.005, remaining))
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        _terminate_process_tree(process, force=True)
        raise
    return timed_out, output_limit_exceeded


def _execute_command(
    command: list[str],
    *,
    workdir: Path | None,
    timeout: float,
    env: Mapping[str, str] | None,
    store: ContentStore | None,
    creationflags: int,
    started: float,
) -> CommandResult:
    with tempfile.TemporaryDirectory(prefix="aer-command-") as temporary_name:
        temporary = Path(temporary_name)
        stdout_capture = _Capture(temporary / "stdout.bin")
        stderr_capture = _Capture(temporary / "stderr.bin")
        sanitized_log = temporary / "sanitized.log"
        budget = _CaptureBudget(_OUTPUT_LIMIT_BYTES)
        try:
            process = subprocess.Popen(
                command,
                cwd=workdir,
                env=dict(env) if env is not None else None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=os.name == "posix",
                creationflags=creationflags,
            )
        except FileNotFoundError as exc:
            raise AerError(
                "DEPENDENCY_MISSING",
                f"Command executable was not found: {command[0]}",
                operation="command.run",
                target=command[0],
            ) from exc
        except OSError as exc:
            raise AerError(
                "COMMAND_FAILED",
                f"Command could not be started: {exc}",
                operation="command.run",
                target=command[0],
            ) from exc

        assert process.stdout is not None
        assert process.stderr is not None
        readers = [
            threading.Thread(
                target=_drain_stream,
                args=(process.stdout, stdout_capture, budget),
                name="aer-stdout-reader",
                daemon=True,
            ),
            threading.Thread(
                target=_drain_stream,
                args=(process.stderr, stderr_capture, budget),
                name="aer-stderr-reader",
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()

        timed_out, output_limit_exceeded = _wait_for_process(
            process,
            budget,
            started=started,
            timeout=timeout,
        )
        deadline = started + timeout
        for reader in readers:
            reader.join(timeout=max(0.0, deadline - time.monotonic()))
        if any(reader.is_alive() for reader in readers):
            # A descendant can inherit a pipe after the direct child exits. Terminate
            # the original process group at the same overall deadline. An exited direct
            # child is not evidence that the command tree has completed.
            timed_out = timed_out or time.monotonic() >= deadline
            _terminate_process_tree(process, force=True)
            cleanup_deadline = time.monotonic() + 1.0
            for reader in readers:
                reader.join(timeout=max(0.0, cleanup_deadline - time.monotonic()))
        if any(reader.is_alive() for reader in readers):  # pragma: no cover - OS failure
            raise AerError(
                "COMMAND_FAILED",
                "Command output streams did not close after process termination.",
                operation="command.run",
            )

        capture_error = stdout_capture.error or stderr_capture.error
        if capture_error is not None:
            raise AerError(
                "COMMAND_FAILED",
                f"Command output could not be captured: {capture_error}",
                operation="command.run",
            ) from capture_error

        output_limit_exceeded = output_limit_exceeded or budget.exceeded.is_set()
        duration_ms = round((time.monotonic() - started) * 1000)
        stdout_last, stderr_last, diagnostics = _write_sanitized_log(
            stdout_capture.path,
            stderr_capture.path,
            sanitized_log,
        )
        raw_ref: str | None = None
        if store is not None:
            with sanitized_log.open("rb") as handle:
                stored = store.put_stream(
                    handle,
                    filename=f"command-{int(started * 1000)}.log",
                    mime_type="text/plain; charset=utf-8",
                    source={
                        "operation": "command.run",
                        "redacted": True,
                        "ansi_removed": True,
                        "progress_preserved": True,
                        "output_limit_bytes": _OUTPUT_LIMIT_BYTES,
                        "output_limit_exceeded": output_limit_exceeded,
                    },
                )
            raw_ref = stored.ref

        summary = stdout_last if stdout_last is not None else stderr_last
        exit_code = process.returncode if process.returncode is not None else -1
        context = (
            []
            if exit_code == 0 and not timed_out and not output_limit_exceeded
            else diagnostics.failure_context()
        )
        return CommandResult(
            exit_code=exit_code,
            duration_ms=duration_ms,
            timed_out=timed_out,
            summary=summary,
            failure_context=context,
            warnings=diagnostics.warning_counts(),
            raw_ref=raw_ref,
            bytes_captured=budget.total_bytes,
            log_truncated=False,
            output_limit_exceeded=output_limit_exceeded,
        )


def run_command(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
    store: ContentStore | None = None,
) -> CommandResult:
    """Execute an argv array with no shell and compact its diagnostic output.

    Output is spooled to private temporary files instead of retained in memory. A command
    that emits more than 256 MiB across stdout and stderr is terminated immediately. Captured
    text is drained, UTF-8-normalized, ANSI-stripped, sectioned, redacted, and persisted before
    the result is returned. The result explicitly reports a capture-limit termination.
    """

    if isinstance(argv, (str, bytes)) or not argv:
        raise AerError(
            "INVALID_ARGUMENT",
            "Command must be a non-empty argv sequence, not a shell string.",
            operation="command.run",
            target="argv",
        )
    command = [str(part) for part in argv]
    if any(not part or "\x00" in part for part in command):
        raise AerError(
            "INVALID_ARGUMENT",
            "Command arguments must be non-empty and may not contain NUL bytes.",
            operation="command.run",
            target="argv",
        )
    if not math.isfinite(timeout) or timeout <= 0:
        raise AerError(
            "INVALID_ARGUMENT",
            "Timeout must be a finite value greater than zero.",
            operation="command.run",
            target="timeout",
        )
    workdir = Path(cwd).expanduser().resolve() if cwd is not None else None
    if workdir is not None and not workdir.is_dir():
        raise AerError(
            "NOT_FOUND",
            "Command working directory does not exist or is not a directory.",
            operation="command.run",
            target=str(workdir),
        )

    creationflags = 0
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        creationflags = 0x00000200  # CREATE_NEW_PROCESS_GROUP
    started = time.monotonic()
    return _execute_command(
        command,
        workdir=workdir,
        timeout=timeout,
        env=env,
        store=store,
        creationflags=creationflags,
        started=started,
    )


def command_response(result: CommandResult) -> dict[str, object]:
    """Convert a :class:`CommandResult` to the repository-wide response protocol."""

    details = result.to_dict()
    metrics = Metrics(duration_ms=result.duration_ms, bytes_read=result.bytes_captured)
    if result.ok:
        return success(
            "command.run",
            {
                "exit_code": result.exit_code,
                "summary": result.summary,
                "raw_ref": result.raw_ref,
                "log_truncated": result.log_truncated,
                "output_limit_exceeded": result.output_limit_exceeded,
            },
            artifacts=([{"ref": result.raw_ref, "role": "raw_log"}] if result.raw_ref else []),
            warnings=[asdict(item) for item in result.warnings],
            metrics=metrics,
        )
    code = (
        "COMMAND_TIMEOUT"
        if result.timed_out
        else "LIMIT_EXCEEDED"
        if result.output_limit_exceeded
        else "COMMAND_FAILED"
    )
    return {
        "ok": False,
        "operation": "command.run",
        "code": code,
        "message": (
            "Command exceeded its timeout."
            if result.timed_out
            else f"Command output exceeded the {_OUTPUT_LIMIT_BYTES}-byte limit."
            if result.output_limit_exceeded
            else "Command exited unsuccessfully."
        ),
        "target": None,
        "details": details,
        "suggested_action": "Inspect failure_context or retrieve raw_ref for the sanitized log.",
        "raw_ref": result.raw_ref,
    }


def _debug_json(result: CommandResult) -> str:  # pragma: no cover - developer helper
    return json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":"))
