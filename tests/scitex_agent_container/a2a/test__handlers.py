"""Tests for a2a/_handlers — handle_echo / claude_cli / claude_session / exec.

Heavy externals are mocked:
    subprocess.run for claude_cli + exec
    claude_agent_sdk import + query() async-gen for claude_session
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scitex_agent_container.a2a import _handlers as h


@pytest.fixture(autouse=True)
def _home_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop SAC_/SCITEX_AGENT_CONTAINER_ A2A vars so handlers see defaults."""
    for key in list(sys.modules.get("os").environ if False else []):
        pass  # placeholder; we mutate via monkeypatch below.
    import os

    for key in list(os.environ):
        if "A2A_" in key or "SAC_A2A" in key:
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# handle_echo
# ---------------------------------------------------------------------------


def test_handle_echo_returns_canned_reply() -> None:
    out = h.handle_echo("alpha", "hi")
    assert "alpha" in out
    assert "hi" in out
    assert "echo handler" in out


# ---------------------------------------------------------------------------
# handle_claude_cli
# ---------------------------------------------------------------------------


def _fake_completed(stdout: str = "hello", stderr: str = "", rc: int = 0):
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.stdout = stdout
    cp.stderr = stderr
    cp.returncode = rc
    return cp


def test_handle_claude_cli_returns_stdout() -> None:
    with patch.object(h.subprocess, "run", return_value=_fake_completed("hi")):
        out = h.handle_claude_cli("alpha", "say hi")
    assert out == "hi"


def test_handle_claude_cli_empty_stdout_yields_placeholder() -> None:
    with patch.object(h.subprocess, "run", return_value=_fake_completed("")):
        out = h.handle_claude_cli("alpha", "x")
    assert out == "(empty response)"


def test_handle_claude_cli_passes_model_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAC_A2A_CLAUDE_MODEL", "sonnet-4")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _fake_completed("ok")

    with patch.object(h.subprocess, "run", side_effect=fake_run):
        h.handle_claude_cli("alpha", "x")
    assert "--model" in captured["cmd"]
    assert "sonnet-4" in captured["cmd"]


def test_handle_claude_cli_not_found_raises_handler_error() -> None:
    with patch.object(h.subprocess, "run", side_effect=FileNotFoundError("nope")):
        with pytest.raises(h.HandlerError, match="claude CLI not found"):
            h.handle_claude_cli("alpha", "x")


def test_handle_claude_cli_timeout_raises_handler_error() -> None:
    err = subprocess.TimeoutExpired(cmd="claude", timeout=1)
    with patch.object(h.subprocess, "run", side_effect=err):
        with pytest.raises(h.HandlerError, match="timeout"):
            h.handle_claude_cli("alpha", "x")


def test_handle_claude_cli_nonzero_rc_raises() -> None:
    with patch.object(h.subprocess, "run", return_value=_fake_completed("", "boom", 2)):
        with pytest.raises(h.HandlerError, match="rc=2"):
            h.handle_claude_cli("alpha", "x")


# ---------------------------------------------------------------------------
# _agent_mcp_servers_and_cwd backwards-compat shim
# ---------------------------------------------------------------------------


def test_agent_mcp_servers_and_cwd_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    from scitex_agent_container.runtimes import _sdk_common

    called: list[str] = []

    def _fake(name):
        called.append(name)
        return ({"mcp": 1}, "/work")

    monkeypatch.setattr(_sdk_common, "resolve_agent_workspace", _fake)
    out = h._agent_mcp_servers_and_cwd("alpha")
    assert out == ({"mcp": 1}, "/work")
    assert called == ["alpha"]


# ---------------------------------------------------------------------------
# handle_claude_session — SDK mocked via fake module
# ---------------------------------------------------------------------------


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Msg:
    def __init__(self, blocks: list[_Block]) -> None:
        self.content = blocks


def _patch_sdk(monkeypatch: pytest.MonkeyPatch, msgs: list, raise_on_query=None):
    mod = types.ModuleType("claude_agent_sdk")
    mod.AssistantMessage = _Msg  # type: ignore[attr-defined]
    mod.TextBlock = _Block  # type: ignore[attr-defined]

    async def _query(prompt: str, options):
        if raise_on_query is not None:
            raise raise_on_query
        for m in msgs:
            yield m

    mod.query = _query  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return mod


def _patch_build_options(monkeypatch: pytest.MonkeyPatch) -> None:
    from scitex_agent_container.runtimes import _sdk_common

    def _fake(name, system_prompt=None, model=None):
        return types.SimpleNamespace(name=name, system=system_prompt, model=model)

    monkeypatch.setattr(_sdk_common, "build_sdk_options", _fake)


def test_handle_claude_session_returns_joined_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sdk(monkeypatch, [_Msg([_Block("hello"), _Block(" world")])])
    _patch_build_options(monkeypatch)
    out = h.handle_claude_session("alpha", "say hi")
    assert "hello" in out
    assert "world" in out


def test_handle_claude_session_empty_returns_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sdk(monkeypatch, [])
    _patch_build_options(monkeypatch)
    out = h.handle_claude_session("alpha", "x")
    assert out == "(empty response)"


def test_handle_claude_session_missing_sdk_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If claude_agent_sdk cannot be imported, raise HandlerError."""
    monkeypatch.delitem(sys.modules, "claude_agent_sdk", raising=False)
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name == "claude_agent_sdk":
            raise ImportError("not here")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    with pytest.raises(h.HandlerError, match="claude-agent-sdk"):
        h.handle_claude_session("alpha", "x")


def test_handle_claude_session_build_options_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sdk(monkeypatch, [])
    from scitex_agent_container.runtimes import _sdk_common

    def _bad(name, **kw):
        raise _sdk_common.SDKCommonError("nope")

    monkeypatch.setattr(_sdk_common, "build_sdk_options", _bad)
    with pytest.raises(h.HandlerError, match="nope"):
        h.handle_claude_session("alpha", "x")


def test_handle_claude_session_query_raises_wraps_to_handler_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sdk(monkeypatch, [], raise_on_query=RuntimeError("api down"))
    _patch_build_options(monkeypatch)
    with pytest.raises(h.HandlerError, match="claude_session failed"):
        h.handle_claude_session("alpha", "x")


def test_handle_claude_session_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """When query takes longer than timeout, raise HandlerError(timeout)."""
    # Force CLAUDE_TIMEOUT_S to a tiny value via module attribute patch.
    monkeypatch.setattr(h, "CLAUDE_TIMEOUT_S", 0.05)

    mod = types.ModuleType("claude_agent_sdk")
    mod.AssistantMessage = _Msg  # type: ignore[attr-defined]
    mod.TextBlock = _Block  # type: ignore[attr-defined]

    async def _slow(prompt, options):
        await asyncio.sleep(1.0)
        if False:
            yield  # pragma: no cover

    mod.query = _slow  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    _patch_build_options(monkeypatch)
    with pytest.raises(h.HandlerError, match="timeout"):
        h.handle_claude_session("alpha", "x")


# ---------------------------------------------------------------------------
# handle_exec
# ---------------------------------------------------------------------------


def test_handle_exec_requires_env_var() -> None:
    with pytest.raises(h.HandlerError, match="SAC_A2A_EXEC_COMMAND is not set"):
        h.handle_exec("alpha", "x")


def test_handle_exec_invalid_shell_word_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAC_A2A_EXEC_COMMAND", "unterminated 'quote")
    with pytest.raises(h.HandlerError, match="could not parse"):
        h.handle_exec("alpha", "x")


def test_handle_exec_empty_argv_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """When shlex parses to empty list, raise HandlerError."""
    # shlex.split('""') returns [''], which has len 1 — to force empty,
    # patch shlex.split.
    monkeypatch.setenv("SAC_A2A_EXEC_COMMAND", "anything")
    monkeypatch.setattr(h.shlex, "split", lambda _s: [])
    with pytest.raises(h.HandlerError, match="empty argv"):
        h.handle_exec("alpha", "x")


def test_handle_exec_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAC_A2A_EXEC_COMMAND", "/bin/echo hi")
    with patch.object(h.subprocess, "run", return_value=_fake_completed("output")):
        out = h.handle_exec("alpha", "ignored")
    assert out == "output"


def test_handle_exec_empty_output_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAC_A2A_EXEC_COMMAND", "/bin/true")
    with patch.object(h.subprocess, "run", return_value=_fake_completed("")):
        out = h.handle_exec("alpha", "x")
    assert out == "(empty response)"


def test_handle_exec_command_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAC_A2A_EXEC_COMMAND", "/no/such/bin")
    with patch.object(h.subprocess, "run", side_effect=FileNotFoundError("nope")):
        with pytest.raises(h.HandlerError, match="exec command not found"):
            h.handle_exec("alpha", "x")


def test_handle_exec_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAC_A2A_EXEC_COMMAND", "/bin/sleep 99")
    err = subprocess.TimeoutExpired(cmd="sleep", timeout=1)
    with patch.object(h.subprocess, "run", side_effect=err):
        with pytest.raises(h.HandlerError, match="timeout"):
            h.handle_exec("alpha", "x")


def test_handle_exec_nonzero_rc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAC_A2A_EXEC_COMMAND", "/bin/false")
    with patch.object(
        h.subprocess, "run", return_value=_fake_completed("", "stderr blob", 17)
    ):
        with pytest.raises(h.HandlerError, match="rc=17"):
            h.handle_exec("alpha", "x")


def test_handle_exec_passes_agent_name_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAC_A2A_EXEC_COMMAND", "/bin/echo hi")
    captured = {}

    def fake_run(argv, **kw):
        captured["env"] = kw.get("env", {})
        return _fake_completed("ok")

    with patch.object(h.subprocess, "run", side_effect=fake_run):
        h.handle_exec("delta", "stdin")
    assert captured["env"].get("SAC_A2A_AGENT") == "delta"


# ---------------------------------------------------------------------------
# HANDLERS registry
# ---------------------------------------------------------------------------


def test_handlers_registry_contains_all_four_keys() -> None:
    assert set(h.HANDLERS) == {"echo", "claude_session", "claude_cli", "exec"}
    assert h.HANDLERS["echo"] is h.handle_echo
    assert h.HANDLERS["claude_cli"] is h.handle_claude_cli
    assert h.HANDLERS["claude_session"] is h.handle_claude_session
    assert h.HANDLERS["exec"] is h.handle_exec
