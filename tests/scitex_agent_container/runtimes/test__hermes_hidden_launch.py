"""Unit tests for starting the Hermes agent tree with SAC hidden.

Real processes, no mocks: a stand-in ``hermes`` console script records what
it received. The namespace mask itself is proven in a real container by
tests/integration/test_sandbox_apptainer_live.py.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from scitex_agent_container._sandbox._hide import HIDE_SCRIPT, hide_argv
from scitex_agent_container.runtimes._hermes_hidden_launch import (
    SECRET_FD_ENV,
    TOKEN_ENV,
    gateway_bootstrap_argv,
    gateway_ready_dir,
    hidden_spawner,
    spawn_hidden_gateway,
    start_gateway,
)

RECORDER = """\
import ctypes, json, os, sys
libc = ctypes.CDLL(None)
dumpable = libc.prctl(3, 0, 0, 0, 0)
libc.prctl(4, 1, 0, 0, 0)  # only so this recorder may read its own environ
with open(os.environ["RECORD_TO"], "w") as fh:
    json.dump({
        "argv": sys.argv,
        "token": os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN"),
        "exec_env": open("/proc/self/environ", "rb").read().decode(errors="replace").split("\\0"),
        "dumpable": dumpable,
    }, fh)
"""


def _fake_hermes(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "hermes"
    script.write_text(f"#!{sys.executable}\n{RECORDER}")
    script.chmod(0o755)
    return bindir


def _env(tmp_path: Path, bindir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["RECORD_TO"] = str(tmp_path / "record.json")
    return env


def _bootstrapped_record(tmp_path: Path, cwd: Path | None = None) -> dict:
    """Run the gateway bootstrap (without the namespace launcher) and record."""
    bindir = _fake_hermes(tmp_path)
    env = _env(tmp_path, bindir)
    read_fd, write_fd = os.pipe()
    os.write(write_fd, f"{TOKEN_ENV}=tok-1234567890abcdef".encode())
    os.close(write_fd)
    env[SECRET_FD_ENV] = str(read_fd)
    import subprocess

    argv = gateway_bootstrap_argv(["hermes", "serve", "--x"], path=env["PATH"])
    subprocess.run(
        argv, env=env, pass_fds=(read_fd,), cwd=cwd, check=True, timeout=60
    )
    os.close(read_fd)
    return json.loads((tmp_path / "record.json").read_text())


@pytest.fixture
def record(tmp_path):
    return _bootstrapped_record(tmp_path)


def test_bootstrap_runs_the_script_with_its_own_arguments(record, tmp_path):
    # Arrange
    expected = [str(tmp_path / "bin" / "hermes"), "serve", "--x"]
    # Act
    argv = record["argv"]
    # Assert
    assert argv == expected


def test_bootstrap_puts_the_key_into_the_runtime_environment(record):
    # Arrange
    # Act
    token = record["token"]
    # Assert
    assert token == "tok-1234567890abcdef"


def test_bootstrap_keeps_the_key_out_of_the_exec_environment(record):
    # Arrange
    # Act
    exposed = [e for e in record["exec_env"] if "tok-1234567890abcdef" in e]
    # Assert
    assert exposed == []


def test_bootstrap_uses_the_scripts_own_interpreter(tmp_path):
    # Arrange
    bindir = _fake_hermes(tmp_path)
    # Act
    argv = gateway_bootstrap_argv(["hermes", "serve"], path=str(bindir))
    # Assert
    assert argv[:2] == [sys.executable, "-c"]


def test_bootstrap_refuses_a_non_python_gateway(tmp_path):
    # Arrange
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "hermes").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "hermes").chmod(0o755)
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="not a Python console script"):
        gateway_bootstrap_argv(["hermes", "serve"], path=str(bindir))


def test_ready_dir_is_the_state_dir_without_hiding(tmp_path):
    # Arrange
    state = tmp_path / "state"
    # Act
    ready = gateway_ready_dir(state, hidden=[])
    # Assert
    assert ready == state


def test_ready_dir_leaves_the_state_dir_when_hiding(tmp_path):
    # Arrange
    state = tmp_path / "state"
    # Act
    ready = gateway_ready_dir(state, hidden=[str(state)])
    # Assert
    assert state not in (ready, *ready.parents)


def test_hidden_spawner_wraps_the_command_in_the_launcher():
    # Arrange
    spawn = hidden_spawner(["/state/a"])
    # Act
    proc = spawn(["true"], env={"PATH": os.environ.get("PATH", "")})
    proc.wait(timeout=60)
    # Assert
    assert proc.args[1:] == ["-I", HIDE_SCRIPT, "--hide", "/state/a", "--", "true"]


def test_bootstrap_makes_the_gateway_non_dumpable(record):
    # Arrange
    # Act
    dumpable = record["dumpable"]
    # Assert
    assert dumpable == 0


def test_hidden_gateway_argv_carries_no_key(tmp_path):
    # Arrange
    bindir = _fake_hermes(tmp_path)
    env = _env(tmp_path, bindir)
    # Act
    proc = spawn_hidden_gateway(
        ["hermes", "serve"],
        env=env,
        secret=(TOKEN_ENV, "tok-hidden-0123456789"),
        hidden=["/state/a"],
    )
    proc.wait(timeout=60)
    # Assert
    assert "tok-hidden-0123456789" not in " ".join(proc.args)


def test_plain_gateway_receives_the_key_in_its_environment(tmp_path):
    # Arrange
    bindir = _fake_hermes(tmp_path)
    env = _env(tmp_path, bindir)
    # Act
    gateway, _spawn = start_gateway(env=env, token="tok-plain-0123456789", hidden=[])
    gateway.wait(timeout=60)
    # Assert
    assert (
        json.loads((tmp_path / "record.json").read_text())["token"]
        == "tok-plain-0123456789"
    )


PLANTED = "import pathlib; pathlib.Path({marker!r}).write_text('planted ran')\n"


def _plant(directory: Path, module: str, marker: Path) -> None:
    """A module an agent could write into the owner's working directory."""
    target = directory / module
    if module.endswith("/__init__.py"):
        target.parent.mkdir(parents=True)
    target.write_text(PLANTED.format(marker=str(marker)))


def test_bootstrap_never_imports_a_module_planted_in_its_working_dir(tmp_path):
    # Arrange
    planted, marker = tmp_path / "agent-writable", tmp_path / "marker"
    planted.mkdir()
    _plant(planted, "runpy.py", marker)
    _plant(planted, "ctypes/__init__.py", marker)
    # Act
    _bootstrapped_record(tmp_path, cwd=planted)
    # Assert
    assert not marker.exists()


def test_launcher_never_imports_a_module_planted_in_its_working_dir(tmp_path):
    # Arrange
    import subprocess

    planted, marker = tmp_path / "agent-writable", tmp_path / "marker"
    planted.mkdir()
    for module in ("glob.py", "argparse.py", "scitex_agent_container/__init__.py"):
        _plant(planted, module, marker)
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(planted)}
    argv = hide_argv(sys.executable, ["/state/a"], ["true"])
    # Act
    subprocess.run(argv, env=env, cwd=planted, timeout=60, check=False)
    # Assert
    assert not marker.exists()


OWNER_SYS_PATH_PROBE = """\
import os, sys
sys.path.insert(0, os.getcwd())
sys.path.insert(0, "")
from scitex_agent_container.runtimes._hermes_hidden_launch import forget_working_dir_imports
forget_working_dir_imports()
print(int(os.getcwd() in sys.path or "" in sys.path))
"""


def test_sealed_owner_drops_its_working_dir_from_sys_path(tmp_path):
    # Arrange
    import subprocess

    import scitex_agent_container

    src = Path(scitex_agent_container.__file__).resolve().parents[1]
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(src)}
    # Act
    out = subprocess.run(
        [sys.executable, "-c", OWNER_SYS_PATH_PROBE],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    # Assert
    assert out.stdout.strip() == "0"
