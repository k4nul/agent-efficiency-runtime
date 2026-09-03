from __future__ import annotations

import json
from pathlib import Path

from aer.config import Settings
from aer.limits import DEFAULT_OUTPUT_BYTES, DISCOVER_OUTPUT_BYTES
from aer.protocol import execute, success
from aer.store import ObjectStore


def test_protocol_stores_and_compacts_oversized_response(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("AER_HOME", str(tmp_path / "home"))
    payload = success("test.large", {"rows": [{"value": "x" * 2000}] * 20})

    assert execute("test.large", lambda: payload) == 0

    output = capsys.readouterr().out
    assert len(output.encode("utf-8")) <= DEFAULT_OUTPUT_BYTES
    compact = json.loads(output)
    assert compact["result"]["truncated"] is True
    raw_ref = compact["result"]["raw_ref"]
    stored = ObjectStore(Settings.load()).get_bytes(raw_ref)
    assert json.loads(stored)["result"]["rows"] == payload["result"]["rows"]


def test_protocol_full_flag_explicitly_allows_large_response(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("AER_HOME", str(tmp_path / "home"))
    payload = success("test.large", {"text": "x" * (DEFAULT_OUTPUT_BYTES + 1)})

    assert execute("test.large", lambda: payload, full=True) == 0

    assert len(capsys.readouterr().out.encode("utf-8")) > DEFAULT_OUTPUT_BYTES


def test_discover_uses_stricter_two_kib_budget(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("AER_HOME", str(tmp_path / "home"))
    payload = success(
        "discover",
        {
            "query": "a",
            "capabilities": [
                {"name": f"capability.{index}", "summary": "s" * 200} for index in range(20)
            ],
        },
    )

    assert execute("discover", lambda: payload) == 0

    output = capsys.readouterr().out
    compact = json.loads(output)
    assert len(output.encode("utf-8")) <= DISCOVER_OUTPUT_BYTES
    assert compact["result"]["capabilities"]
    assert compact["result"]["capabilities_count"] == 20
    assert compact["result"]["raw_ref"].startswith("aer://sha256/")
