from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

import aer.runner.command as runner_command
from aer.config import Settings
from aer.errors import AerError
from aer.runner import command_response, redact_secrets, run_command
from aer.store import ObjectStore


@dataclass
class _Stored:
    ref: str


class _MemoryStore:
    def __init__(self) -> None:
        self.data = b""
        self.source: dict[str, object] | None = None

    def put_stream(
        self,
        stream: object,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        source: dict[str, object] | None = None,
    ) -> _Stored:
        assert hasattr(stream, "read")
        self.data = stream.read()  # type: ignore[union-attr]
        self.source = source
        return _Stored("aer://sha256/" + "a" * 64)


def _python(code: str, *arguments: str) -> list[str]:
    return [sys.executable, "-c", code, *arguments]


def test_success_returns_only_last_summary_and_sanitized_raw_log() -> None:
    store = _MemoryStore()
    result = run_command(
        _python(
            "import sys; "
            "print('first successful test'); "
            "print('100%'); "
            "print('\\x1b[32m12 passed\\x1b[0m'); "
            "print('WARNING repeated', file=sys.stderr); "
            "print('WARNING repeated', file=sys.stderr)"
        ),
        store=store,
    )

    assert result.ok
    assert result.summary == "12 passed"
    assert result.failure_context == []
    assert [(item.message, item.count) for item in result.warnings] == [("WARNING repeated", 2)]
    assert result.raw_ref == "aer://sha256/" + "a" * 64
    assert b"first successful test" in store.data
    assert b"\x1b" not in store.data
    assert b"100%" not in store.data
    assert store.source == {
        "operation": "command.run",
        "redacted": True,
        "output_limit_bytes": 256 * 1024 * 1024,
        "output_limit_exceeded": False,
    }
    response = command_response(result)
    assert response["ok"] is True
    assert "first successful test" not in json.dumps(response)


def test_failure_context_is_bounded_for_five_thousand_lines() -> None:
    store = _MemoryStore()
    result = run_command(
        _python(
            "import sys; "
            "[print(f'ordinary line {i}') for i in range(5000)]; "
            "print('ERROR exact failure marker'); "
            "sys.exit(7)"
        ),
        store=store,
    )

    assert not result.ok
    assert result.exit_code == 7
    assert any("ERROR exact failure marker" in line for line in result.failure_context)
    assert len(result.failure_context) <= 80
    assert len("\n".join(result.failure_context).encode()) <= 16 * 1024
    assert b"ordinary line 0" in store.data
    response = command_response(result)
    assert response["code"] == "COMMAND_FAILED"
    assert len(json.dumps(response).encode()) <= 16 * 1024


def test_single_huge_lines_cannot_break_the_response_budget() -> None:
    result = run_command(
        _python(
            "import sys; "
            "print('WARNING ' + 'w' * 100000, file=sys.stderr); "
            "print('ERROR ' + 'e' * 100000); "
            "sys.exit(1)"
        )
    )
    response = command_response(result)
    assert len(result.summary.encode()) <= 1024
    assert len(result.warnings[0].message.encode()) <= 256
    assert len(json.dumps(response).encode()) <= 16 * 1024


def test_secrets_are_redacted_in_preview_and_stored_log() -> None:
    secret_values = [
        "sk-abcdefghijklmnopqrstuvwxyz",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "AKIAIOSFODNN7EXAMPLE",
        "database://user:super-secret@localhost/db",
        "top-secret-password",
        "session-token-secret",
        "cookie-value; Path=/; HttpOnly",
        "private-key-material",
    ]
    store = _MemoryStore()
    script = """
import sys
print('Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz')
print('password=top-secret-password')
print('github ghp_abcdefghijklmnopqrstuvwxyz123456')
print('aws AKIAIOSFODNN7EXAMPLE')
print('AWS_SESSION_TOKEN=session-token-secret')
print('Set-Cookie: cookie-value; Path=/; HttpOnly')
print('-----BEGIN PRIVATE KEY-----')
print('private-key-material')
print('-----END PRIVATE KEY-----')
print('database://user:super-secret@localhost/db')
print('ERROR request failed', file=sys.stderr)
sys.exit(1)
"""
    result = run_command(_python(script), store=store)
    serialized = json.dumps(result.to_dict())
    stored = store.data.decode()

    for secret in secret_values:
        assert secret not in serialized
        assert secret not in stored
    assert "[REDACTED" in stored


def test_private_key_multiline_redaction() -> None:
    private_key = """-----BEGIN PRIVATE KEY-----
abc123
def456
-----END PRIVATE KEY-----"""
    assert private_key not in redact_secrets(private_key)
    assert redact_secrets(private_key) == "[REDACTED PRIVATE KEY]"


def test_json_and_environment_secret_names_are_redacted() -> None:
    value = (
        "OPENAI_API_KEY=plain-value\nGITHUB_TOKEN=another-value\n"
        '{"authorization":"Bearer json-token","password":"json-password"}'
    )
    redacted = redact_secrets(value)
    for secret in ("plain-value", "another-value", "json-token", "json-password"):
        assert secret not in redacted


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Cookie: sid=cookie-secret; theme=dark", "Cookie: [REDACTED]"),
        (
            "INFO < Set-Cookie: session=set-cookie-secret; Path=/; HttpOnly",
            "INFO < Set-Cookie: [REDACTED]",
        ),
        ("aws_access_key_id = AKIAIOSFODNN7EXAMPLE", "aws_access_key_id = [REDACTED]"),
        (
            "aws_secret_access_key=secret-access-key-value",
            "aws_secret_access_key=[REDACTED]",
        ),
        ("AWS_SESSION_TOKEN opaquesessionvalue", "AWS_SESSION_TOKEN [REDACTED]"),
        ("AWS_SECURITY_TOKEN opaquesecurityvalue", "AWS_SECURITY_TOKEN [REDACTED]"),
        ("x-amz-security-token: amz-token-value", "x-amz-security-token: [REDACTED]"),
        ("ASIAIOSFODNN7EXAMPLE", "[REDACTED TOKEN]"),
        (
            "Authorization: AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE, "
            "Signature=signature-secret",
            "Authorization: [REDACTED]",
        ),
    ],
)
def test_cookie_and_common_aws_forms_are_fully_redacted(value: str, expected: str) -> None:
    assert redact_secrets(value) == expected


def test_cookie_and_aws_secrets_are_redacted_in_preview_and_raw_log() -> None:
    secrets = [
        "cookie-secret",
        "second-cookie-secret",
        "AKIAIOSFODNN7EXAMPLE",
        "secret-access-key-value",
        "session-token-value+/=",
        "security-token-value+/=",
        "ASIAIOSFODNN7EXAMPLE",
        "signature-secret",
    ]
    store = _MemoryStore()
    script = """
import sys
lines = [
    'ERROR Cookie: sid=cookie-secret; theme=second-cookie-secret',
    'ERROR Set-Cookie: sid=second-cookie-secret; Path=/; HttpOnly',
    'ERROR AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE',
    'ERROR AWS_SECRET_ACCESS_KEY=secret-access-key-value',
    'ERROR AWS_SESSION_TOKEN=session-token-value+/=',
    'ERROR AWS_SECURITY_TOKEN=security-token-value+/=',
    'ERROR AWS_ACCESS_KEY=ASIAIOSFODNN7EXAMPLE',
    'ERROR Authorization: AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE, Signature=signature-secret',
]
for line in lines:
    print(line, file=sys.stderr)
sys.exit(1)
"""

    result = run_command(_python(script), store=store)
    preview = json.dumps(result.to_dict())
    raw = store.data.decode()

    assert result.ok is False
    for secret in secrets:
        assert secret not in preview
        assert secret not in raw
    assert "Path=/" not in preview
    assert "Path=/" not in raw
    assert "Signature=" not in preview
    assert "Signature=" not in raw
    assert "ERROR Cookie: [REDACTED]" in raw
    assert "ERROR Set-Cookie: [REDACTED]" in raw


def test_argv_is_not_interpreted_by_a_shell(tmp_path: Path) -> None:
    marker = tmp_path / "injected"
    hostile = f"; touch {marker}"
    result = run_command(_python("import sys; print(sys.argv[1])", hostile))

    assert result.ok
    assert result.summary == hostile
    assert not marker.exists()


def test_runner_uses_requested_working_directory(tmp_path: Path) -> None:
    result = run_command(_python("import os; print(os.getcwd())"), cwd=tmp_path)
    assert result.summary == str(tmp_path)


def test_runner_drains_stdout_and_stderr_without_deadlock() -> None:
    result = run_command(
        _python(
            "import sys; "
            "sys.stdout.write('o' * 200000); sys.stdout.flush(); "
            "sys.stderr.write('e' * 200000); sys.stderr.flush(); "
            "print('\\ndone')"
        ),
        timeout=10,
    )
    assert result.ok
    assert result.bytes_captured >= 400_000
    assert result.summary == "done"


def test_raw_ref_preserves_sanitized_output_beyond_old_32_mib_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AER_HOME", str(tmp_path / "home"))
    store = ObjectStore(Settings.load())
    script = """
import sys
chunk = b'x' * 65535 + b'\\n'
for _ in range(513):
    sys.stdout.buffer.write(chunk)
sys.stdout.buffer.write(b'OPENAI_API_KEY=tail-secret-value\\n')
"""

    result = run_command(_python(script), store=store, timeout=20)

    assert result.ok
    assert result.bytes_captured > 32 * 1024 * 1024
    assert result.log_truncated is False
    assert result.output_limit_exceeded is False
    assert result.raw_ref is not None
    raw = store.get_bytes(result.raw_ref)
    assert len(raw) > 32 * 1024 * 1024
    assert raw.count(b"\n") == 515
    assert raw.endswith(b"OPENAI_API_KEY=[REDACTED]\n")
    assert b"tail-secret-value" not in raw


def test_output_limit_is_fail_fast_but_captured_log_is_not_truncated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner_command, "_OUTPUT_LIMIT_BYTES", 64 * 1024)
    monkeypatch.setenv("AER_HOME", str(tmp_path / "home"))
    store = ObjectStore(Settings.load())
    script = """
import sys
chunk = b'generated-before-stop\\n' * 1024
while True:
    sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
"""

    result = run_command(_python(script), store=store, timeout=5)

    assert result.ok is False
    assert result.output_limit_exceeded is True
    assert result.log_truncated is False
    assert result.raw_ref is not None
    raw = store.get_bytes(result.raw_ref)
    assert raw.startswith(b"[stdout]\n")
    assert b"generated-before-stop" in raw
    assert command_response(result)["code"] == "LIMIT_EXCEEDED"


def test_timeout_returns_compact_timeout_result() -> None:
    result = run_command(_python("import time; time.sleep(30)"), timeout=0.1)
    assert result.timed_out
    assert not result.ok
    assert command_response(result)["code"] == "COMMAND_TIMEOUT"


@pytest.mark.parametrize("argv", [[], "echo unsafe"])
def test_runner_rejects_non_argv_commands(argv: object) -> None:
    with pytest.raises(AerError, match="argv sequence"):
        run_command(argv)  # type: ignore[arg-type]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group behavior")
def test_timeout_terminates_child_process_group(tmp_path: Path) -> None:
    child_pid = tmp_path / "child.pid"
    script = """
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(30)
"""
    result = run_command(_python(script, str(child_pid)), timeout=0.5)
    assert result.timed_out
    pid = int(child_pid.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group behavior")
def test_timeout_applies_when_exited_parent_leaves_inherited_pipe(tmp_path: Path) -> None:
    child_pid = tmp_path / "orphan.pid"
    script = """
import pathlib, subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
"""
    started = time.monotonic()

    result = run_command(_python(script, str(child_pid)), timeout=0.2)

    assert time.monotonic() - started < 2
    assert result.timed_out is True
    assert command_response(result)["code"] == "COMMAND_TIMEOUT"
    pid = int(child_pid.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
