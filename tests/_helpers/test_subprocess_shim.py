"""Sanity tests for the subprocess_shim + env_save_restore fixtures."""

from __future__ import annotations

import os
import subprocess


def test_subprocess_shim_returns_configured_exit_zero(subprocess_shim):
    # Arrange
    subprocess_shim.install("fake_ssh", stdout="hello", exit=0)
    # Act
    result = subprocess.run(["fake_ssh"], capture_output=True, text=True, check=False)
    # Assert
    assert result.returncode == 0


def test_subprocess_shim_emits_configured_stdout(subprocess_shim):
    # Arrange
    subprocess_shim.install("fake_ssh", stdout="hello", exit=0)
    # Act
    result = subprocess.run(["fake_ssh"], capture_output=True, text=True, check=False)
    # Assert
    assert result.stdout == "hello"


def test_subprocess_shim_records_full_argv(subprocess_shim):
    # Arrange
    subprocess_shim.install("fake_ssh")
    # Act
    subprocess.run(
        ["fake_ssh", "-J", "mba", "user@host", "--", "echo", "ok"], check=False
    )
    # Assert
    assert subprocess_shim.argv_for("fake_ssh") == [
        "-J",
        "mba",
        "user@host",
        "--",
        "echo",
        "ok",
    ]


def test_subprocess_shim_counts_repeated_invocations(subprocess_shim):
    # Arrange
    subprocess_shim.install("fake_tmux")
    # Act
    subprocess.run(["fake_tmux", "has-session", "-t", "alpha"], check=False)
    subprocess.run(["fake_tmux", "list-panes", "-t", "alpha"], check=False)
    # Assert
    assert subprocess_shim.call_count("fake_tmux") == 2


def test_subprocess_shim_preserves_invocation_order(subprocess_shim):
    # Arrange
    subprocess_shim.install("fake_tmux")
    # Act
    subprocess.run(["fake_tmux", "has-session"], check=False)
    subprocess.run(["fake_tmux", "list-panes"], check=False)
    # Assert
    assert subprocess_shim.invocations("fake_tmux") == [
        ["has-session"],
        ["list-panes"],
    ]


def test_subprocess_shim_propagates_nonzero_exit_code(subprocess_shim):
    # Arrange
    subprocess_shim.install("fake_ssh", exit=255, stderr="timeout")
    # Act
    result = subprocess.run(["fake_ssh"], capture_output=True, text=True, check=False)
    # Assert
    assert result.returncode == 255


def test_subprocess_shim_emits_configured_stderr(subprocess_shim):
    # Arrange
    subprocess_shim.install("fake_ssh", exit=1, stderr="timeout")
    # Act
    result = subprocess.run(["fake_ssh"], capture_output=True, text=True, check=False)
    # Assert
    assert result.stderr == "timeout"


def test_env_save_restore_set_writes_to_real_environ(env_save_restore):
    # Arrange
    # (clean state — no prior value for the test key)
    # Act
    env_save_restore.set("SAC_SHIM_TEST_VAR", "hello")
    # Assert
    assert os.environ["SAC_SHIM_TEST_VAR"] == "hello"


def test_env_save_restore_reverts_set_value_on_teardown():
    # Arrange
    # (previous test set SAC_SHIM_TEST_VAR; this test runs after teardown)
    # Act
    value = os.environ.get("SAC_SHIM_TEST_VAR")
    # Assert
    assert value is None
