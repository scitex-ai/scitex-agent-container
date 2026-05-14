"""Tests for a2a/_handlers — no mocks (PA-306).

External processes are exercised via REAL shim scripts on disk
(``tmp_path`` + a small executable). The `claude_session` handler is
intentionally NOT unit-tested here — it's covered end-to-end by
``tests/integration/test_workflow_a2a_live.py`` which drives a real
``claude-agent-sdk`` turn against a live ``sac a2a serve``. Mocked
unit tests for that path were the source of multiple false-positives;
removing them rather than re-mocking is the honest fix.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from scitex_agent_container.a2a import _handlers as h

# ---------------------------------------------------------------------------
# Fixtures — real isolation via tmp_path + a controlled env dict.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path):
    """Snapshot $HOME-pointed-at-tmp + drop SAC_A2A env. Cleanup on teardown.

    Replaces the prior ``monkeypatch.setattr(Path, "home", ...)`` +
    ``monkeypatch.delenv`` chain with explicit save/restore — no
    ``monkeypatch`` fixture, no `unittest.mock`.
    """
    saved = {k: os.environ.get(k) for k in list(os.environ)}

    # Point HOME at tmp_path so tests that resolve user paths land there.
    os.environ["HOME"] = str(tmp_path)
    # Drop any SAC_A2A_* / SAC_A2A * env that would leak into the
    # handler under test.
    for key in list(os.environ):
        if "A2A_" in key or key.startswith("SAC_A2A"):
            os.environ.pop(key, None)

    yield tmp_path

    # Restore.
    for k in list(os.environ):
        if k not in saved:
            os.environ.pop(k, None)
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _write_shim(
    tmp_path: Path,
    name: str,
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    sleep_seconds: float = 0.0,
) -> Path:
    """Write a real executable shim that emits canned output + exit code.

    Replaces ``MagicMock(spec=subprocess.CompletedProcess)``. The shim
    is a tiny bash script — running it through ``subprocess.run`` from
    the real handler gives us a genuine CompletedProcess with the
    fields we want, without ever touching `unittest.mock`.
    """
    script = tmp_path / name
    body = "#!/usr/bin/env bash\n"
    if sleep_seconds > 0:
        body += f"sleep {sleep_seconds}\n"
    if stdout:
        # Use printf %s so multi-line stdout is preserved literally.
        body += f"printf '%s' {_sh_quote(stdout)}\n"
    if stderr:
        body += f"printf '%s' {_sh_quote(stderr)} 1>&2\n"
    body += f"exit {exit_code}\n"
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# handle_echo — pure function
# ---------------------------------------------------------------------------


def test_handle_echo_returns_canned_reply() -> None:
    out = h.handle_echo("alpha", "hi")
    assert "alpha" in out
    assert "hi" in out
    assert "echo handler" in out


# ---------------------------------------------------------------------------
# handle_claude_cli — uses a real shim instead of mocking subprocess.run
# ---------------------------------------------------------------------------


def test_handle_claude_cli_returns_stdout(isolated_env: Path) -> None:
    bin_ = _write_shim(isolated_env, "fake-claude", stdout="hi")
    os.environ["SAC_A2A_CLAUDE_BIN"] = str(bin_)
    assert h.handle_claude_cli("alpha", "say hi") == "hi"


def test_handle_claude_cli_empty_stdout_yields_placeholder(
    isolated_env: Path,
) -> None:
    bin_ = _write_shim(isolated_env, "fake-claude", stdout="")
    os.environ["SAC_A2A_CLAUDE_BIN"] = str(bin_)
    assert h.handle_claude_cli("alpha", "x") == "(empty response)"


def test_handle_claude_cli_passes_model_when_env_set(isolated_env: Path) -> None:
    # Shim records its argv into a file so the test can inspect it.
    record = isolated_env / "argv.txt"
    bin_ = isolated_env / "fake-claude"
    bin_.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$@\" > {_sh_quote(str(record))}\n"
        "printf '%s' ok\n"
    )
    bin_.chmod(bin_.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.environ["SAC_A2A_CLAUDE_BIN"] = str(bin_)
    os.environ["SAC_A2A_CLAUDE_MODEL"] = "sonnet-4"

    h.handle_claude_cli("alpha", "x")

    argv = record.read_text().splitlines()
    assert "--model" in argv
    assert "sonnet-4" in argv


def test_handle_claude_cli_not_found_raises(isolated_env: Path) -> None:
    os.environ["SAC_A2A_CLAUDE_BIN"] = str(isolated_env / "does-not-exist")
    with pytest.raises(h.HandlerError, match="claude CLI not found"):
        h.handle_claude_cli("alpha", "x")


def test_handle_claude_cli_timeout_raises(isolated_env: Path) -> None:
    bin_ = _write_shim(isolated_env, "slow-claude", sleep_seconds=2.0)
    os.environ["SAC_A2A_CLAUDE_BIN"] = str(bin_)
    os.environ["SAC_A2A_CLAUDE_TIMEOUT_S"] = "0.2"
    # We have to re-import the handler module so the new timeout env
    # is read (CLAUDE_TIMEOUT_S is module-level).
    import importlib

    importlib.reload(h)
    try:
        with pytest.raises(h.HandlerError, match="timeout"):
            h.handle_claude_cli("alpha", "x")
    finally:
        os.environ.pop("SAC_A2A_CLAUDE_TIMEOUT_S", None)
        importlib.reload(h)


def test_handle_claude_cli_nonzero_rc_raises(isolated_env: Path) -> None:
    bin_ = _write_shim(
        isolated_env, "fake-claude", stdout="", stderr="boom", exit_code=2
    )
    os.environ["SAC_A2A_CLAUDE_BIN"] = str(bin_)
    with pytest.raises(h.HandlerError, match="rc=2"):
        h.handle_claude_cli("alpha", "x")


# ---------------------------------------------------------------------------
# _agent_mcp_servers_and_cwd — backwards-compat shim, real workspace
# ---------------------------------------------------------------------------


def test_agent_mcp_servers_and_cwd_unknown_agent_returns_empty(
    isolated_env: Path,
) -> None:
    """For an agent NOT registered in the Registry, the shim returns
    the documented ``({}, None)`` fallback — no exception, no spurious
    workspace. Tests the happy path of the not-registered branch.

    A round-trip test with a REAL Registry entry belongs in the
    living integration suite (it needs ``sac agent register`` and a
    real config) — that's where we exercise the populated path, not
    here. This unit verifies the "unknown agent" contract.
    """
    mcp, cwd = h._agent_mcp_servers_and_cwd("never-registered")
    assert mcp == {}
    assert cwd is None


# ---------------------------------------------------------------------------
# handle_claude_session — covered by tests/integration/test_workflow_a2a_live.py
# ---------------------------------------------------------------------------
# We do NOT unit-test handle_claude_session here. Mocking
# `claude_agent_sdk.query()` was the exact pattern that let multiple
# false-positives ship (system prompt forbidding tools, missing
# permission_mode, wrong listen URL). The living integration test
# covers the real wiring; that's the honest contract.


# ---------------------------------------------------------------------------
# handle_exec — uses a real shim binary
# ---------------------------------------------------------------------------


def test_handle_exec_requires_env_var(isolated_env: Path) -> None:
    with pytest.raises(h.HandlerError, match="SAC_A2A_EXEC_COMMAND is not set"):
        h.handle_exec("alpha", "x")


def test_handle_exec_invalid_shell_word_raises(isolated_env: Path) -> None:
    os.environ["SAC_A2A_EXEC_COMMAND"] = "unterminated 'quote"
    with pytest.raises(h.HandlerError, match="could not parse"):
        h.handle_exec("alpha", "x")


def test_handle_exec_happy_path(isolated_env: Path) -> None:
    bin_ = _write_shim(isolated_env, "fake-exec", stdout="output")
    os.environ["SAC_A2A_EXEC_COMMAND"] = str(bin_)
    assert h.handle_exec("alpha", "ignored") == "output"


def test_handle_exec_empty_output_placeholder(isolated_env: Path) -> None:
    bin_ = _write_shim(isolated_env, "fake-exec", stdout="")
    os.environ["SAC_A2A_EXEC_COMMAND"] = str(bin_)
    assert h.handle_exec("alpha", "x") == "(empty response)"


def test_handle_exec_command_not_found(isolated_env: Path) -> None:
    os.environ["SAC_A2A_EXEC_COMMAND"] = str(isolated_env / "no-such-binary")
    with pytest.raises(h.HandlerError, match="exec command not found"):
        h.handle_exec("alpha", "x")


def test_handle_exec_timeout(isolated_env: Path) -> None:
    bin_ = _write_shim(isolated_env, "slow-exec", sleep_seconds=2.0)
    os.environ["SAC_A2A_EXEC_COMMAND"] = str(bin_)
    os.environ["SAC_A2A_EXEC_TIMEOUT_S"] = "0.2"
    import importlib

    importlib.reload(h)
    try:
        with pytest.raises(h.HandlerError, match="timeout"):
            h.handle_exec("alpha", "x")
    finally:
        os.environ.pop("SAC_A2A_EXEC_TIMEOUT_S", None)
        importlib.reload(h)


def test_handle_exec_nonzero_rc(isolated_env: Path) -> None:
    bin_ = _write_shim(
        isolated_env, "fake-exec", stdout="", stderr="stderr blob", exit_code=17
    )
    os.environ["SAC_A2A_EXEC_COMMAND"] = str(bin_)
    with pytest.raises(h.HandlerError, match="rc=17"):
        h.handle_exec("alpha", "x")


def test_handle_exec_passes_agent_name_via_env(isolated_env: Path) -> None:
    """The handler must export SAC_A2A_AGENT into the child's env."""
    record = isolated_env / "agent.txt"
    bin_ = isolated_env / "fake-exec"
    bin_.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s' \"$SAC_A2A_AGENT\" > {_sh_quote(str(record))}\n"
        "printf '%s' ok\n"
    )
    bin_.chmod(bin_.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.environ["SAC_A2A_EXEC_COMMAND"] = str(bin_)
    h.handle_exec("delta", "stdin")
    assert record.read_text() == "delta"


# ---------------------------------------------------------------------------
# HANDLERS registry — pure structural assertions
# ---------------------------------------------------------------------------


def test_handlers_registry_contains_all_four_keys() -> None:
    assert set(h.HANDLERS) == {"echo", "claude_session", "claude_cli", "exec"}
    assert h.HANDLERS["echo"] is h.handle_echo
    assert h.HANDLERS["claude_cli"] is h.handle_claude_cli
    assert h.HANDLERS["claude_session"] is h.handle_claude_session
    assert h.HANDLERS["exec"] is h.handle_exec


# Keep ``sys`` referenced for tooling that doesn't follow ``# noqa`` on imports
# in some checkers — we no longer monkeypatch sys.modules but the import is
# harmless to retain.
_ = sys
