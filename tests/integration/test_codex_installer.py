from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = PROJECT_ROOT / "integrations" / "codex" / "install.sh"


def _run_installer(
    target: Path | None,
    *,
    path: str,
    home: Path,
    mode: str = "copy",
    codex_home: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update({"HOME": str(home), "PATH": path})
    if codex_home is None:
        environment.pop("CODEX_HOME", None)
    else:
        environment["CODEX_HOME"] = str(codex_home)
    argv = [str(INSTALLER), f"--{mode}"]
    if target is not None:
        argv.extend(("--target", str(target)))
    return subprocess.run(
        argv,
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


@pytest.mark.parametrize("use_codex_home", [False, True])
def test_codex_installer_uses_default_codex_location(tmp_path: Path, use_codex_home: bool) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    aer = tools / "aer"
    aer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    aer.chmod(0o755)
    home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    configured_codex_home = codex_home if use_codex_home else None

    completed = _run_installer(
        None,
        path=os.pathsep.join((str(tools), os.defpath)),
        home=home,
        codex_home=configured_codex_home,
    )

    expected_home = codex_home if use_codex_home else home / ".codex"
    destination = expected_home / "skills" / "agent-efficiency-runtime"
    assert completed.returncode == 0, completed.stderr
    assert (destination / "SKILL.md").is_file()
    unexpected_home = home / ".codex" if use_codex_home else codex_home
    assert not unexpected_home.exists()


def test_codex_installer_symlinks_once_and_refuses_collision(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("Symbolic links are required")
    tools = tmp_path / "tools"
    tools.mkdir()
    aer = tools / "aer"
    aer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    aer.chmod(0o755)
    target = tmp_path / "skills"
    installer_path = os.pathsep.join((str(tools), os.defpath))

    first = _run_installer(
        target,
        path=installer_path,
        home=tmp_path / "home",
        mode="symlink",
    )
    destination = target / "agent-efficiency-runtime"

    assert first.returncode == 0, first.stderr
    assert destination.is_symlink()
    assert destination.resolve() == INSTALLER.parent.resolve()
    assert (destination / "SKILL.md").is_file()

    second = _run_installer(
        target,
        path=installer_path,
        home=tmp_path / "home",
        mode="symlink",
    )
    assert second.returncode == 3
    assert "Refusing to overwrite existing path" in second.stderr
    assert destination.is_symlink()
