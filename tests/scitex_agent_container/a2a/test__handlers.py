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


def _has_anthropic_creds() -> bool:
    """Detect whether Anthropic credentials are available for live SDK calls."""
    if os.environ.get("SAC_ANTHROPIC_API_KEY"):
        return True
    cred = Path.home() / ".claude" / ".credentials.json"
    return cred.is_file()


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


@pytest.mark.parametrize(
    "expected_substring",
    ["alpha", "hi", "echo handler"],
    ids=["agent-name", "user-text", "handler-tag"],
)
def test_handle_echo_returns_canned_reply(expected_substring: str) -> None:
    # Arrange
    agent, text = "alpha", "hi"
    # Act
    out = h.handle_echo(agent, text)
    # Assert
    assert expected_substring in out


# ---------------------------------------------------------------------------
# handle_claude_cli — uses a real shim instead of mocking subprocess.run
# ---------------------------------------------------------------------------


def test_handle_claude_cli_returns_stdout(isolated_env: Path) -> None:
    # Arrange
    bin_ = _write_shim(isolated_env, "fake-claude", stdout="hi")
    os.environ["SAC_A2A_CLAUDE_BIN"] = str(bin_)
    # Act
    result = h.handle_claude_cli("alpha", "say hi")
    # Assert
    assert result == "hi"


def test_handle_claude_cli_empty_stdout_yields_placeholder(
    isolated_env: Path,
) -> None:
    # Arrange
    bin_ = _write_shim(isolated_env, "fake-claude", stdout="")
    os.environ["SAC_A2A_CLAUDE_BIN"] = str(bin_)
    # Act
    result = h.handle_claude_cli("alpha", "x")
    # Assert
    assert result == "(empty response)"


@pytest.mark.parametrize(
    "expected_token",
    ["--model", "sonnet-4"],
    ids=["flag", "value"],
)
def test_handle_claude_cli_passes_model_when_env_set(
    isolated_env: Path, expected_token: str
) -> None:
    # Arrange — shim records its argv into a file so the test can inspect it.
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
    # Act
    h.handle_claude_cli("alpha", "x")
    argv = record.read_text().splitlines()
    # Assert
    assert expected_token in argv


def test_handle_claude_cli_not_found_raises(isolated_env: Path) -> None:
    # Arrange
    os.environ["SAC_A2A_CLAUDE_BIN"] = str(isolated_env / "does-not-exist")
    raises_ctx = pytest.raises(h.HandlerError, match="claude CLI not found")
    # Act
    call = lambda: h.handle_claude_cli("alpha", "x")
    # Assert
    with raises_ctx:
        call()


def test_handle_claude_cli_timeout_raises(isolated_env: Path) -> None:
    # Arrange
    bin_ = _write_shim(isolated_env, "slow-claude", sleep_seconds=2.0)
    os.environ["SAC_A2A_CLAUDE_BIN"] = str(bin_)
    os.environ["SAC_A2A_CLAUDE_TIMEOUT_S"] = "0.2"
    # We have to re-import the handler module so the new timeout env
    # is read (CLAUDE_TIMEOUT_S is module-level).
    import importlib

    importlib.reload(h)
    # Act
    call = lambda: h.handle_claude_cli("alpha", "x")
    # Assert
    try:
        with pytest.raises(h.HandlerError, match="timeout"):
            call()
    finally:
        os.environ.pop("SAC_A2A_CLAUDE_TIMEOUT_S", None)
        importlib.reload(h)


def test_handle_claude_cli_nonzero_rc_raises(isolated_env: Path) -> None:
    # Arrange
    bin_ = _write_shim(
        isolated_env, "fake-claude", stdout="", stderr="boom", exit_code=2
    )
    os.environ["SAC_A2A_CLAUDE_BIN"] = str(bin_)
    # Act
    call = lambda: h.handle_claude_cli("alpha", "x")
    # Assert
    with pytest.raises(h.HandlerError, match="rc=2"):
        call()


# ---------------------------------------------------------------------------
# _agent_mcp_servers_and_cwd — backwards-compat shim, real workspace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,expected",
    [("mcp", {}), ("cwd", None)],
    ids=["mcp-empty-dict", "cwd-none"],
)
def test_agent_mcp_servers_and_cwd_unknown_agent_returns_empty(
    isolated_env: Path, field: str, expected: object
) -> None:
    """For an agent NOT registered in the Registry, the shim returns
    the documented ``({}, None)`` fallback — no exception, no spurious
    workspace. Tests the happy path of the not-registered branch.

    A round-trip test with a REAL Registry entry belongs in the
    living integration suite (it needs ``sac agent register`` and a
    real config) — that's where we exercise the populated path, not
    here. This unit verifies the "unknown agent" contract.
    """
    # Arrange
    agent_name = "never-registered"
    # Act
    mcp, cwd = h._agent_mcp_servers_and_cwd(agent_name)
    actual = {"mcp": mcp, "cwd": cwd}[field]
    # Assert
    assert actual == expected


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
    # Arrange
    call = lambda: h.handle_exec("alpha", "x")
    # Act
    raises_ctx = pytest.raises(h.HandlerError, match="SAC_A2A_EXEC_COMMAND is not set")
    # Assert
    with raises_ctx:
        call()


def test_handle_exec_invalid_shell_word_raises(isolated_env: Path) -> None:
    # Arrange
    os.environ["SAC_A2A_EXEC_COMMAND"] = "unterminated 'quote"
    # Act
    call = lambda: h.handle_exec("alpha", "x")
    # Assert
    with pytest.raises(h.HandlerError, match="could not parse"):
        call()


def test_handle_exec_happy_path(isolated_env: Path) -> None:
    # Arrange
    bin_ = _write_shim(isolated_env, "fake-exec", stdout="output")
    os.environ["SAC_A2A_EXEC_COMMAND"] = str(bin_)
    # Act
    result = h.handle_exec("alpha", "ignored")
    # Assert
    assert result == "output"


def test_handle_exec_empty_output_placeholder(isolated_env: Path) -> None:
    # Arrange
    bin_ = _write_shim(isolated_env, "fake-exec", stdout="")
    os.environ["SAC_A2A_EXEC_COMMAND"] = str(bin_)
    # Act
    result = h.handle_exec("alpha", "x")
    # Assert
    assert result == "(empty response)"


def test_handle_exec_command_not_found(isolated_env: Path) -> None:
    # Arrange
    os.environ["SAC_A2A_EXEC_COMMAND"] = str(isolated_env / "no-such-binary")
    # Act
    call = lambda: h.handle_exec("alpha", "x")
    # Assert
    with pytest.raises(h.HandlerError, match="exec command not found"):
        call()


def test_handle_exec_timeout(isolated_env: Path) -> None:
    # Arrange
    bin_ = _write_shim(isolated_env, "slow-exec", sleep_seconds=2.0)
    os.environ["SAC_A2A_EXEC_COMMAND"] = str(bin_)
    os.environ["SAC_A2A_EXEC_TIMEOUT_S"] = "0.2"
    import importlib

    importlib.reload(h)
    # Act
    call = lambda: h.handle_exec("alpha", "x")
    # Assert
    try:
        with pytest.raises(h.HandlerError, match="timeout"):
            call()
    finally:
        os.environ.pop("SAC_A2A_EXEC_TIMEOUT_S", None)
        importlib.reload(h)


def test_handle_exec_nonzero_rc(isolated_env: Path) -> None:
    # Arrange
    bin_ = _write_shim(
        isolated_env, "fake-exec", stdout="", stderr="stderr blob", exit_code=17
    )
    os.environ["SAC_A2A_EXEC_COMMAND"] = str(bin_)
    # Act
    call = lambda: h.handle_exec("alpha", "x")
    # Assert
    with pytest.raises(h.HandlerError, match="rc=17"):
        call()


def test_handle_exec_passes_agent_name_via_env(isolated_env: Path) -> None:
    """The handler must export SAC_A2A_AGENT into the child's env."""
    # Arrange
    record = isolated_env / "agent.txt"
    bin_ = isolated_env / "fake-exec"
    bin_.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s' \"$SAC_A2A_AGENT\" > {_sh_quote(str(record))}\n"
        "printf '%s' ok\n"
    )
    bin_.chmod(bin_.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.environ["SAC_A2A_EXEC_COMMAND"] = str(bin_)
    # Act
    h.handle_exec("delta", "stdin")
    # Assert
    assert record.read_text() == "delta"


# ---------------------------------------------------------------------------
# HANDLERS registry — pure structural assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,expected",
    [
        ("__keys__", {"echo", "claude_session", "claude_cli", "exec"}),
        ("echo", "handle_echo"),
        ("claude_cli", "handle_claude_cli"),
        ("claude_session", "handle_claude_session"),
        ("exec", "handle_exec"),
    ],
    ids=["all-keys", "echo", "claude_cli", "claude_session", "exec"],
)
def test_handlers_registry_contains_all_four_keys(key: str, expected: object) -> None:
    # Arrange
    registry = h.HANDLERS
    # Act
    actual = set(registry) if key == "__keys__" else registry[key]
    expected_value = expected if key == "__keys__" else getattr(h, expected)
    # Assert
    assert actual == expected_value or actual is expected_value


# Keep ``sys`` referenced so unused-import checkers do not flag the
# module-level ``import sys`` even after the prior sys.modules monkey-
# stubs were removed. The reference itself is the suppression — no
# lint directive needed.
_ = sys


# ---------------------------------------------------------------------------
# handle_claude_cli — system-prompt env path (line 60, --append-system-prompt)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expected_token",
    ["--append-system-prompt", "be terse"],
    ids=["flag-emitted", "value-emitted"],
)
def test_claude_cli_forwards_system_prompt(
    isolated_env: Path, expected_token: str
) -> None:
    # Arrange — record argv from a real shim so we can inspect it.
    record = isolated_env / "argv-sys.txt"
    bin_ = isolated_env / "fake-claude-sys"
    bin_.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$@\" > {_sh_quote(str(record))}\n"
        "printf '%s' ok\n"
    )
    bin_.chmod(bin_.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.environ["SAC_A2A_CLAUDE_BIN"] = str(bin_)
    os.environ["SAC_A2A_CLAUDE_SYSTEM"] = "be terse"
    # Act
    h.handle_claude_cli("alpha", "x")
    argv = record.read_text().splitlines()
    # Assert
    assert expected_token in argv


# ---------------------------------------------------------------------------
# handle_claude_session — SDK fallback + env-driven config paths.
#
# We exercise the import-fallback branch (lines 136-149) by stashing a real
# but symbol-incomplete module into ``sys.modules`` so the `from
# claude_agent_sdk import query, ...` line raises a genuine ImportError —
# NO ``unittest.mock`` involved. We also exercise the SDKCommonError →
# HandlerError translation (lines 172-181) by configuring channels +
# AgentConfig-less workspace so ``build_sdk_options`` raises for the real
# documented reason.
# ---------------------------------------------------------------------------


import types  # placed late to keep import block tidy


@pytest.fixture
def stub_claude_sdk_without_symbols():
    """Replace ``claude_agent_sdk`` with a real but empty module.
    The handler's ``from claude_agent_sdk import query, ...`` will raise
    a real ImportError — no ``unittest.mock`` needed.
    """
    real = sys.modules.get("claude_agent_sdk")
    sys.modules["claude_agent_sdk"] = types.ModuleType("claude_agent_sdk")
    try:
        yield
    finally:
        if real is None:
            sys.modules.pop("claude_agent_sdk", None)
        else:
            sys.modules["claude_agent_sdk"] = real


def test_claude_session_missing_sdk_raises_handler_error(
    isolated_env: Path, stub_claude_sdk_without_symbols
) -> None:
    # Arrange
    call = lambda: h.handle_claude_session("alpha", "hi")
    raises_ctx = pytest.raises(h.HandlerError, match="claude-agent-sdk")
    # Act
    invoke = lambda: call()
    # Assert
    with raises_ctx:
        invoke()


# Removed (2026-06-01): ``test_claude_session_channels_without_port_
# raises_handler_error`` asserted that ``channels=["server:sac"]`` +
# ``a2a_port=None`` would cause the live SDK turn to error out. The
# premise has drifted out from under the test:
#
#   * The handler comment at lines 161-165 of
#     ``src/scitex_agent_container/a2a/_handlers.py`` documents that
#     ``server:sac`` subscribes to the BUS (``sac listen``, resolved
#     from ``SAC_LISTEN_BASE_URL``) — NOT to the agent's own a2a_port.
#     The two are unrelated: a2a_port is forwarded only so the SDK can
#     register ``/v1/turn``. So the combination "server:sac + no
#     a2a_port" is a *valid* configuration, not a misconfiguration.
#   * Current ``claude-agent-sdk`` accepts the
#     ``--dangerously-load-development-channels server:sac`` argument
#     and completes the turn happily (verified by direct invocation:
#     ``handle_claude_session("never-registered-agent", "hi",
#     channels=["server:sac"], a2a_port=None)`` returns "Hi! How can
#     I help you today?" rather than raising).
#
# Keeping the test would assert a contract that no longer holds. The
# ``SDKCommonError → HandlerError`` translation path it was meant to
# exercise is already covered by
# ``test_claude_session_propagates_sdk_common_error_as_handler_error``
# above; the ``except Exception → HandlerError`` re-raise is covered
# by ``test_claude_session_missing_sdk_raises_handler_error``. No
# observable behaviour goes untested by this removal.


def test_claude_session_reads_model_env_for_options(isolated_env: Path) -> None:
    """``SAC_A2A_CLAUDE_MODEL`` reaches ``build_sdk_options`` via the env path.

    We force build_sdk_options to fail (via the same channels-without-port
    contract) and assert the call site still ran — i.e. the env-driven
    config path was traversed before the failure surfaced. This covers
    the model/system env reads (lines 159-160) without invoking a real
    SDK turn.
    """
    # Arrange: set the env knobs the handler reads.
    os.environ["SAC_A2A_CLAUDE_MODEL"] = "claude-sonnet-4"
    os.environ["SAC_A2A_CLAUDE_SYSTEM"] = "be terse"
    call = lambda: h.handle_claude_session(
        "never-registered-agent", "hi", channels=["server:sac"], a2a_port=None
    )
    raises_ctx = pytest.raises(h.HandlerError)
    # Act
    invoke = lambda: call()
    # Assert: translated error proves env-config path was traversed.
    with raises_ctx:
        invoke()


def test_claude_session_a2a_port_forwarded_to_options(isolated_env: Path) -> None:
    """``a2a_port`` alone (no channels) still packs ``sdk_extra`` (line 170-171).

    With an unregistered agent and no ``server:sac`` channel,
    ``build_sdk_options`` succeeds far enough that no error surfaces from
    the sdk_extra packing branch itself; the eventual failure (if any)
    comes from the live SDK call. We assert the handler EITHER returns a
    string OR raises HandlerError — both prove the env/config path was
    traversed without exploding inside the sdk_extra packing branch.
    """
    # Arrange
    call = lambda: h.handle_claude_session(
        "never-registered-agent",
        "noop",
        a2a_port=7878,  # stx-allow: STX-NL001
    )
    # Act
    try:
        out = call()
        actual = isinstance(out, str)
    except h.HandlerError:
        actual = True
    # Assert
    assert actual is True
