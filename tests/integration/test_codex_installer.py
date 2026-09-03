from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = PROJECT_ROOT / "integrations" / "codex" / "install.sh"


def _run_installer(target: Path, *, path: str, home: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update({"HOME": str(home), "PATH": path})
    environment.pop("CODEX_HOME", None)
    return subprocess.run(
        [str(INSTALLER), "--copy", "--target", str(target)],
        env=environment,
        capture_output=True,
        text=True,
        shell=False,
        timeout=10,
        check=False,
    )


def test_codex_installer_requires_aer_on_path(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    dirname = shutil.which("dirname")
    if bash is None or dirname is None or not hasattr(os, "symlink"):
        pytest.skip("POSIX shell tools are required")

    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "bash").symlink_to(bash)
    (tools / "dirname").symlink_to(dirname)
    target = tmp_path / "skills"

    completed = _run_installer(target, path=str(tools), home=tmp_path / "home")

    assert completed.returncode == 1
    assert "aer command is not installed" in completed.stderr
    assert not target.exists()


def test_codex_installer_copies_once_and_refuses_collision(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    aer = tools / "aer"
    aer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    aer.chmod(0o755)
    system_path = os.defpath
    target = tmp_path / "skills"

    first = _run_installer(
        target,
        path=os.pathsep.join((str(tools), system_path)),
        home=tmp_path / "home",
    )
    destination = target / "agent-efficiency-runtime"

    assert first.returncode == 0, first.stderr
    assert (destination / "SKILL.md").is_file()
    sentinel = destination / "sentinel"
    sentinel.write_text("preserve\n", encoding="utf-8")

    second = _run_installer(
        target,
        path=os.pathsep.join((str(tools), system_path)),
        home=tmp_path / "home",
    )

    assert second.returncode == 3
    assert "Refusing to overwrite existing path" in second.stderr
    assert sentinel.read_text(encoding="utf-8") == "preserve\n"


def test_codex_installer_removes_only_its_reserved_path_after_copy_failure(
    tmp_path: Path,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, body in {
        "aer": "#!/bin/sh\nexit 0\n",
        "cp": "#!/bin/sh\nexit 4\n",
    }.items():
        executable = tools / name
        executable.write_text(body, encoding="utf-8")
        executable.chmod(0o755)
    target = tmp_path / "skills"
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("preserve\n", encoding="utf-8")

    completed = _run_installer(
        target,
        path=os.pathsep.join((str(tools), os.defpath)),
        home=tmp_path / "home",
    )

    assert completed.returncode == 4
    assert not (target / "agent-efficiency-runtime").exists()
    assert unrelated.read_text(encoding="utf-8") == "preserve\n"
