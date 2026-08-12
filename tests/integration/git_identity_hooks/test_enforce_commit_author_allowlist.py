"""Regression tests for ``enforce_commit_author_allowlist.sh``.

The hook is the fail-loud backstop for the CLA-author failure class
(incident scitex-hpc 2026-07-05: a green PR blocked by CLAssistant because
its commits were authored ``agent@scitex-hpc``, mapping to no allowlisted
GitHub account). These tests drive the real shell hook against real
ephemeral git repos — no mocks — and assert the block/allow decision plus
the shape of the actionable message.

Located under ``tests/integration/`` (not ``tests/<pkg>/``) because the
hook is a ``.sh`` asset with no ``.py`` source to mirror; PS-204 only
scans the ``tests/<pkg>/`` mirror tree.
"""

from __future__ import annotations

import subprocess

import pytest

from .conftest import (
    AGENT_EMAIL,
    ALLOWLISTED_EMAIL,
    HOOK_SCRIPT,
    NON_ALLOWLISTED_EMAIL,
    run_hook,
)


# --- result fixtures (run the hook once; each test asserts one thing) --


@pytest.fixture
def self_test_result():
    return subprocess.run(
        ["bash", str(HOOK_SCRIPT), "--self-test"], capture_output=True, text=True
    )


@pytest.fixture
def commit_bad_result(bad_repo):
    return run_hook("git commit -m x", bad_repo)


@pytest.fixture
def ambient_env_result(bad_repo):
    return run_hook(
        "git commit -m x", bad_repo, extra_env={"GIT_AUTHOR_EMAIL": "stray@nowhere.invalid"}
    )


# --- asset presence ---------------------------------------------------


def test_hook_script_file_exists():
    # Arrange
    script = HOOK_SCRIPT
    # Act
    present = script.is_file()
    # Assert
    assert present, f"missing hook: {script}"


def test_hook_script_is_executable():
    # Arrange
    script = HOOK_SCRIPT
    # Act
    mode = script.stat().st_mode
    # Assert
    assert mode & 0o111, "hook is not executable"


# --- the script's own self-test (broadest built-in coverage) ----------


def test_self_test_exits_zero(self_test_result):
    # Arrange
    res = self_test_result
    # Act
    rc = res.returncode
    # Assert
    assert rc == 0, res.stdout + res.stderr


def test_self_test_reports_no_failures(self_test_result):
    # Arrange
    res = self_test_result
    # Act
    out = res.stdout
    # Assert
    assert "fail=0" in out


# --- commit: allow / block --------------------------------------------


def test_commit_allowlisted_author_is_allowed(good_repo):
    # Arrange
    cmd = "git commit -m x"
    # Act
    res = run_hook(cmd, good_repo)
    # Assert
    assert res.returncode == 0, res.stderr


def test_commit_non_allowlisted_author_is_blocked(commit_bad_result):
    # Arrange
    res = commit_bad_result
    # Act
    rc = res.returncode
    # Assert
    assert rc == 2


def test_commit_block_message_names_the_bad_email(commit_bad_result):
    # Arrange
    res = commit_bad_result
    # Act
    stderr = res.stderr
    # Assert
    assert NON_ALLOWLISTED_EMAIL in stderr


def test_commit_block_message_distinguishes_from_cla_bot_error(commit_bad_result):
    # Arrange
    res = commit_bad_result
    # Act
    stderr = res.stderr
    # Assert
    assert "NOT a failure of the CLAssistant" in stderr


def test_commit_block_message_gives_config_fix(commit_bad_result):
    # Arrange
    res = commit_bad_result
    # Act
    stderr = res.stderr
    # Assert — the remediation must point at the AGENT identity. The hook
    # only ever runs inside an agent container, so telling it to author as
    # the operator would undo the 2026-08-12 identity split it now enforces.
    assert f"config user.email {AGENT_EMAIL}" in stderr


# --- the agent identity is allowlisted alongside the human one -------


def test_commit_agent_identity_is_allowed(agent_repo):
    # Arrange
    cmd = "git commit -m x"
    # Act
    res = run_hook(cmd, agent_repo)
    # Assert
    assert res.returncode == 0, res.stderr


def test_push_agent_identity_commit_is_allowed(agent_repo):
    # Arrange
    cmd = "git push origin feature/x"
    # Act
    res = run_hook(cmd, agent_repo)
    # Assert
    assert res.returncode == 0, res.stderr


def test_agent_identity_match_is_case_insensitive(bad_repo):
    # Arrange
    cmd = "git -c user.email=Agent@SciTeX.ai commit -m x"
    # Act
    res = run_hook(cmd, bad_repo)
    # Assert
    assert res.returncode == 0, res.stderr


def test_human_identity_still_allowed_alongside_agent(good_repo):
    # Arrange — the agent identity is ADDITIVE; the operator's own commits
    # must keep passing the same gate.
    cmd = f"git -c user.email={ALLOWLISTED_EMAIL} commit -m x"
    # Act
    res = run_hook(cmd, good_repo)
    # Assert
    assert res.returncode == 0, res.stderr


def test_agent_at_hostname_shape_is_still_blocked(bad_repo):
    # Arrange — the incident shape (agent@<host>) must NOT be swept in by
    # allowlisting agent@scitex.ai.
    cmd = f"git -c user.email={NON_ALLOWLISTED_EMAIL} commit -m x"
    # Act
    res = run_hook(cmd, bad_repo)
    # Assert
    assert res.returncode == 2


def test_commit_blocked_when_targeted_via_git_dash_c(bad_repo, tmp_path):
    # Arrange
    cmd = f"git -C {bad_repo} commit -m x"
    # Act
    res = run_hook(cmd, tmp_path)
    # Assert
    assert res.returncode == 2


def test_commit_blocked_in_chained_add_then_commit(bad_repo):
    # Arrange
    cmd = "git add -A && git commit -m x"
    # Act
    res = run_hook(cmd, bad_repo)
    # Assert
    assert res.returncode == 2


# --- inline identity overrides (mirror git precedence) ----------------


def test_inline_author_email_env_overrides_bad_config(bad_repo):
    # Arrange
    cmd = f"GIT_AUTHOR_EMAIL={ALLOWLISTED_EMAIL} git commit -m x"
    # Act
    res = run_hook(cmd, bad_repo)
    # Assert
    assert res.returncode == 0, res.stderr


def test_inline_bad_author_flag_blocked_in_good_repo(good_repo):
    # Arrange
    cmd = "git commit -m x --author='Nobody <nobody@nowhere.invalid>'"
    # Act
    res = run_hook(cmd, good_repo)
    # Assert
    assert res.returncode == 2


def test_ambient_bad_author_email_env_is_blocked(ambient_env_result):
    # Arrange
    res = ambient_env_result
    # Act
    rc = res.returncode
    # Assert
    assert rc == 2


def test_ambient_bad_author_email_message_points_at_env(ambient_env_result):
    # Arrange
    res = ambient_env_result
    # Act
    stderr = res.stderr
    # Assert
    assert "GIT_AUTHOR_EMAIL" in stderr


# --- push: judges the unpushed commits' authors -----------------------


def test_push_with_bad_author_commit_is_blocked(bad_repo):
    # Arrange
    cmd = "git push origin feature/x"
    # Act
    res = run_hook(cmd, bad_repo)
    # Assert
    assert res.returncode == 2


def test_push_with_good_author_commit_is_allowed(good_repo):
    # Arrange
    cmd = "git push origin feature/x"
    # Act
    res = run_hook(cmd, good_repo)
    # Assert
    assert res.returncode == 0, res.stderr


# --- allowlist extension + bypasses -----------------------------------


def test_env_extension_allowlists_extra_email(bad_repo):
    # Arrange
    extra = {"CC_CLA_ALLOWED_EMAILS": NON_ALLOWLISTED_EMAIL}
    # Act
    res = run_hook("git commit -m x", bad_repo, extra_env=extra)
    # Assert
    assert res.returncode == 0, res.stderr


def test_inline_marker_bypasses_the_guard(bad_repo):
    # Arrange
    cmd = "git commit -m x # hook-bypass: cla-author"
    # Act
    res = run_hook(cmd, bad_repo)
    # Assert
    assert res.returncode == 0


def test_env_var_bypasses_the_guard(bad_repo):
    # Arrange
    extra = {"CC_ALLOW_CLA_AUTHOR": "1"}
    # Act
    res = run_hook("git commit -m x", bad_repo, extra_env=extra)
    # Assert
    assert res.returncode == 0


# --- pass-through: read-only / non-git / non-Bash ---------------------


@pytest.mark.parametrize("cmd", ["git status", "git log --oneline", "git add -A", "ls -la"])
def test_readonly_and_non_git_commands_pass_through(bad_repo, cmd):
    # Arrange
    command = cmd
    # Act
    res = run_hook(command, bad_repo)
    # Assert
    assert res.returncode == 0


def test_non_bash_tool_invocation_passes_through():
    # Arrange
    payload = '{"tool_name":"Edit","tool_input":{}}'
    # Act
    res = subprocess.run(
        ["bash", str(HOOK_SCRIPT)], input=payload, capture_output=True, text=True
    )
    # Assert
    assert res.returncode == 0
