"""Supervisor pieces that seal a launch: env flags, path rewrite, process env."""

from __future__ import annotations

import os
from pathlib import Path

import subprocess
import sys

from scitex_agent_container._sandbox._supervisor import (
    EXIT_SETUP,
    SizeWatchdog,
    run_dir_bytes,
    main,
    minimal_process_env,
    rewrite_run_paths,
    seal_env_flags,
)

ALLOWED = frozenset({"KEEP", "MODEL_KEY"})


def test_disallowed_env_flag_is_dropped(tmp_path):
    # Arrange
    argv = ["apptainer", "exec", "--env", "KEEP=1", "--env", "FLEET_ID=x", "img"]
    # Act
    out, _ = seal_env_flags(argv, ALLOWED, tmp_path)
    # Assert
    assert out == ["apptainer", "exec", "--env", "KEEP=1", "img"]


def test_glued_env_flag_is_sealed_too(tmp_path):
    # Arrange
    argv = ["--env=FLEET_ID=x", "--env=KEEP=1"]
    # Act
    out, _ = seal_env_flags(argv, ALLOWED, tmp_path)
    # Assert
    assert out == ["--env", "KEEP=1"]


def test_env_file_is_rewritten_with_allowed_lines_only(tmp_path):
    # Arrange
    src = tmp_path / "in.env"
    src.write_text("MODEL_KEY=v\nGITHUB_TOKEN=g\nexport KEEP=2\n")
    secrets = tmp_path / "secrets"
    out, _ = seal_env_flags(["--env-file", str(src)], ALLOWED, secrets)
    # Act
    text = Path(out[1]).read_text()
    # Assert
    assert text == "MODEL_KEY=v\nKEEP=2\n"


def test_sealed_env_file_is_owner_only(tmp_path):
    # Arrange
    src = tmp_path / "in.env"
    src.write_text("MODEL_KEY=v\n")
    out, _ = seal_env_flags(["--env-file", str(src)], ALLOWED, tmp_path / "s")
    # Act
    mode = os.stat(out[1]).st_mode & 0o777
    # Assert
    assert mode == 0o600


def test_dropped_names_are_reported_without_values(tmp_path):
    # Arrange
    src = tmp_path / "in.env"
    src.write_text("GITHUB_TOKEN=secretvalue\n")
    # Act
    _, names = seal_env_flags(
        ["--env", "X=1", "--env-file", str(src)], ALLOWED, tmp_path
    )
    # Assert
    assert names == {"passed": [], "dropped": ["GITHUB_TOKEN", "X"]}


def test_run_link_paths_are_rewritten_at_boundaries():
    # Arrange
    argv = [
        "--bind",
        "/s/sandbox/run/home:/home/agent",
        "/s/sandbox/runner",
        "--overlay",
        "/s/sandbox/run/ov.img",
    ]
    # Act
    out = rewrite_run_paths(argv, {"/s/sandbox/run": "/scratch/r1"})
    # Assert
    assert out == [
        "--bind",
        "/scratch/r1/home:/home/agent",
        "/s/sandbox/runner",
        "--overlay",
        "/scratch/r1/ov.img",
    ]


def test_process_env_carries_no_host_variable():
    # Arrange
    parent = {
        "TERM": "xterm",
        "SCITEX_STORE_DSN": "x",
        "GITHUB_TOKEN": "y",
        "APPTAINER_BIND": "/a",
    }
    # Act
    env = minimal_process_env(parent)
    # Assert
    assert set(env) == {"PATH", "HOME", "USER", "LOGNAME", "LANG", "TERM"}


def test_unprepared_run_refuses_to_launch(tmp_path):
    # Arrange
    argv = [
        "--state-dir",
        str(tmp_path),
        "--agent",
        "a",
        "--image",
        "x.sif",
        "--",
        "true",
    ]
    # Act
    rc = main(argv)
    # Assert
    assert rc == EXIT_SETUP


def test_run_dir_bytes_ignores_out_and_secrets(tmp_path):
    # Arrange
    for d in ("home", "out", "secrets"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "f").write_bytes(b"x" * 100_000)
    # Act
    used = run_dir_bytes(tmp_path)
    # Assert
    assert 100_000 <= used < 200_000


def test_size_watchdog_terminates_an_oversized_run(tmp_path):
    # Arrange
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "big").write_bytes(b"x" * 2_000_000)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    dog = SizeWatchdog(tmp_path, 1_000_000, child, poll_s=0.1)
    # Act
    dog.start()
    rc = child.wait(timeout=10)
    # Assert
    assert rc != 0 and dog.exceeded_at_bytes >= 2_000_000
