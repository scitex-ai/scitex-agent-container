"""Secret material must never reach the ``claude`` child's command line.

Card ``sac-bot-token-plaintext-in-process-argv-20260814``: a plain
``ps -eo args`` on scitex-compute-04 printed a live Telegram bot token,
because ``claude-agent-sdk`` serialises an ``mcp_servers`` DICT into
``--mcp-config <json>`` — env blocks and all — and ``/proc/<pid>/cmdline`` is
world-readable while ``/proc/<pid>/environ`` is uid-restricted.

THE GUARD THAT ACTUALLY CATCHES IT
==================================
The exposure is a property of the LAUNCHED PROCESS, not of sac's own data
structures, so asserting on ``options.mcp_servers`` alone would keep passing
if the SDK changed how it serialises. :func:`_sdk_argv` therefore drives the
SDK's REAL argv builder (``SubprocessCLITransport._build_command``) — the
exact code that produced the leaked command line — and the assertions search
every argv element for a token-shaped sentinel.

``test_dict_form_puts_the_secret_on_the_child_argv`` pins the OLD behaviour
as still-true of the SDK, so the pair reads as cause and cure: the dict form
leaks, the externalised form does not. Deleting the
``externalize_mcp_servers`` call from ``build_sdk_options`` turns
``test_build_sdk_options_child_argv_carries_no_secret`` red.

The sentinel is a FAKE, deliberately shaped like a Telegram bot token
(``<digits>:<base64ish>``) so it matches the same pattern a real one would.
No real secret appears in this file.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scitex_agent_container.runtimes import _sdk_common
from scitex_agent_container.runtimes._mcp_config_file import (
    MCP_CONFIG_DIR_MODE,
    MCP_CONFIG_FILE_MODE,
    McpConfigWriteError,
    externalize_mcp_servers,
    has_inprocess_servers,
    read_mcp_servers,
    write_mcp_config_file,
)
from scitex_agent_container.runtimes._mcp_spec_env import SPEC_ENV_KEYS_VAR

#: A fake, token-SHAPED value. Never a real credential — the point is that it
#: matches what a scanner looking for a bot token would match.
SENTINEL = "1234567890:FAKE-not-a-real-token-000000000000000"

#: The env key the live leak travelled under (the telegrammer bot token).
SENTINEL_KEY = "CCT_BOT_TOKEN"


def _server_with_secret() -> dict[str, Any]:
    """One stdio MCP entry whose env block carries the sentinel."""
    return {
        "claude-code-telegrammer": {
            "type": "stdio",
            "command": "/usr/bin/true",
            "args": [],
            "env": {SENTINEL_KEY: SENTINEL},
        }
    }


class _Env:
    """Records env / attribute mutations and reverses them (PA-306, no mocks)."""

    def __init__(self) -> None:
        self._env: dict[str, str | None] = {}
        self._attrs: list[tuple[Any, str, Any]] = []

    def setenv(self, key: str, value: str) -> None:
        self._env.setdefault(key, os.environ.get(key))
        os.environ[key] = value

    def delenv(self, key: str) -> None:
        self._env.setdefault(key, os.environ.get(key))
        os.environ.pop(key, None)

    def setattr_module(self, obj: Any, name: str, value: Any) -> None:
        if not any(o is obj and n == name for o, n, _ in self._attrs):
            self._attrs.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self) -> None:
        for key, prev in self._env.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        for obj, name, prev in self._attrs:
            setattr(obj, name, prev)


@pytest.fixture
def env():
    helper = _Env()
    try:
        yield helper
    finally:
        helper.restore()


# ---------------------------------------------------------------------------
# The argv guard — driven through the SDK's own command builder
# ---------------------------------------------------------------------------


def _sdk_argv(mcp_servers: Any) -> list[str]:
    """Return the real ``claude`` child argv the SDK would exec.

    Uses the SDK's own ``SubprocessCLITransport._build_command`` so the
    assertion covers the actual serialisation that leaked, not a
    reimplementation of it. ``cli_path`` short-circuits binary discovery, so
    nothing is spawned and no ``claude`` install is needed.
    """
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    options = ClaudeAgentOptions(cli_path="/usr/bin/true", mcp_servers=mcp_servers)
    return SubprocessCLITransport(prompt="", options=options)._build_command()


def test_dict_form_puts_the_secret_on_the_child_argv() -> None:
    """The defect, pinned: a dict ``mcp_servers`` leaks env values into argv.

    Not a wish — a statement about the installed SDK. If this ever goes red,
    the SDK stopped inlining the config and the fix below may be relaxed;
    until then it is the reason the fix exists.
    """
    # Arrange
    servers = _server_with_secret()
    # Act
    argv = _sdk_argv(servers)
    # Assert
    assert any(SENTINEL in arg for arg in argv)


def test_externalized_form_keeps_the_secret_off_the_child_argv(
    tmp_path: Path,
) -> None:
    # Arrange
    kwargs: dict[str, Any] = {"mcp_servers": _server_with_secret()}
    externalize_mcp_servers(kwargs, "telegrammer-agent")
    # Act
    argv = _sdk_argv(kwargs["mcp_servers"])
    # Assert
    assert not any(SENTINEL in arg for arg in argv)


def test_externalized_form_still_passes_an_mcp_config_flag() -> None:
    """Removing the leak must not remove the config — claude still loads it."""
    # Arrange
    kwargs: dict[str, Any] = {"mcp_servers": _server_with_secret()}
    externalize_mcp_servers(kwargs, "telegrammer-agent")
    # Act
    argv = _sdk_argv(kwargs["mcp_servers"])
    # Assert
    assert "--mcp-config" in argv


def test_externalized_argv_value_is_the_config_path() -> None:
    # Arrange
    kwargs: dict[str, Any] = {"mcp_servers": _server_with_secret()}
    externalize_mcp_servers(kwargs, "telegrammer-agent")
    argv = _sdk_argv(kwargs["mcp_servers"])
    # Act
    value = argv[argv.index("--mcp-config") + 1]
    # Assert
    assert value == kwargs["mcp_servers"]


def test_file_named_in_argv_still_carries_the_server_entry() -> None:
    """The config claude reads is unchanged — only its transport moved."""
    # Arrange
    kwargs: dict[str, Any] = {"mcp_servers": _server_with_secret()}
    externalize_mcp_servers(kwargs, "telegrammer-agent")
    # Act
    servers = read_mcp_servers(kwargs["mcp_servers"])
    # Assert
    assert servers["claude-code-telegrammer"]["env"][SENTINEL_KEY] == SENTINEL


# ---------------------------------------------------------------------------
# File / directory permissions — argv is public, this file must not be
# ---------------------------------------------------------------------------


def test_config_file_is_readable_by_owner_only(tmp_path: Path) -> None:
    # Arrange
    directory = tmp_path / "d"
    # Act
    path = write_mcp_config_file("a", _server_with_secret(), dirs=[directory])
    # Assert
    assert stat.S_IMODE(os.stat(path).st_mode) == MCP_CONFIG_FILE_MODE


def test_config_dir_is_traversable_by_owner_only(tmp_path: Path) -> None:
    # Arrange
    directory = tmp_path / "d"
    # Act
    write_mcp_config_file("a", _server_with_secret(), dirs=[directory])
    # Assert
    assert stat.S_IMODE(os.stat(directory).st_mode) == MCP_CONFIG_DIR_MODE


def test_preexisting_world_readable_file_is_tightened(tmp_path: Path) -> None:
    """An 0644 file left by an older sac must not be reused as-is."""
    # Arrange
    directory = tmp_path / "d"
    directory.mkdir()
    stale = directory / "a.mcp.json"
    stale.write_text("{}")
    os.chmod(stale, 0o644)
    # Act
    path = write_mcp_config_file("a", _server_with_secret(), dirs=[directory])
    # Assert
    assert stat.S_IMODE(os.stat(path).st_mode) == MCP_CONFIG_FILE_MODE


def test_written_payload_uses_the_mcp_json_shape(tmp_path: Path) -> None:
    # Arrange
    directory = tmp_path / "d"
    # Act
    path = write_mcp_config_file("a", _server_with_secret(), dirs=[directory])
    # Assert
    assert "mcpServers" in json.loads(Path(path).read_text())


def test_unwritable_location_raises_instead_of_falling_back(tmp_path: Path) -> None:
    """No silent fallback: the dict form would re-publish every secret."""
    # Arrange: a FILE where the code wants a directory → mkdir raises OSError.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    servers = _server_with_secret()
    # Act
    def write():
        write_mcp_config_file("a", servers, dirs=[blocker / "sub"])

    # Assert
    with pytest.raises(McpConfigWriteError):
        write()


# ---------------------------------------------------------------------------
# externalize_mcp_servers — the narrow no-op cases
# ---------------------------------------------------------------------------


def test_no_servers_is_a_noop() -> None:
    # Arrange
    kwargs: dict[str, Any] = {}
    # Act
    changed = externalize_mcp_servers(kwargs, "a")
    # Assert
    assert changed is False


def test_inprocess_server_declines_the_rewrite() -> None:
    """``type: sdk`` entries hold a live object and cannot be serialised."""
    # Arrange
    kwargs: dict[str, Any] = {
        "mcp_servers": {"inproc": {"type": "sdk", "instance": object()}}
    }
    # Act
    changed = externalize_mcp_servers(kwargs, "a")
    # Assert
    assert changed is False


def test_inprocess_detector_ignores_stdio_entries() -> None:
    # Arrange
    servers = _server_with_secret()
    # Act
    detected = has_inprocess_servers(servers)
    # Assert
    assert detected is False


def test_already_a_path_is_left_alone(tmp_path: Path) -> None:
    # Arrange
    kwargs: dict[str, Any] = {"mcp_servers": str(tmp_path / "x.mcp.json")}
    # Act
    changed = externalize_mcp_servers(kwargs, "a")
    # Assert
    assert changed is False


# ---------------------------------------------------------------------------
# End-to-end: build_sdk_options is where the wiring must live
# ---------------------------------------------------------------------------


def _valid_creds_json() -> str:
    expires_at_ms = int((time.time() + 86_400) * 1_000)
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "tok",
                "refreshToken": "ref",
                "expiresAt": expires_at_ms,
                "scopes": ["user:inference"],
                "subscriptionType": "max",
            }
        }
    )


@pytest.fixture
def _built_options(env: _Env, tmp_path: Path):
    """``build_sdk_options`` for an agent whose MCP entry holds a secret."""
    # Arrange: hermetic auth (no touch of the operator's real credentials).
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / ".credentials.json").write_text(_valid_creds_json())
    env.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))
    env.delenv("ANTHROPIC_API_KEY")
    env.delenv(_sdk_common._SAC_API_KEY_ENV)
    # Arrange: drop the spec-env manifest this test process inherited from
    # its own sac container. Left in place, ``bake_spec_env_into_servers``
    # fails loud on the HOST agent's keys and the fixture errors before it
    # can say anything about argv.
    env.delenv(SPEC_ENV_KEYS_VAR)
    # Arrange: HOME redirected so the merge of $HOME/.mcp.json and the config
    # file this fix writes both stay inside tmp_path.
    home = tmp_path / "home"
    home.mkdir()
    env.setenv("HOME", str(home))
    # Arrange: a registered workspace whose .mcp.json carries the sentinel.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": _server_with_secret()}))

    import scitex_agent_container._state.registry as reg_mod
    import scitex_agent_container.config as cfg_mod

    class _FakeRegistry:
        def get(self, _name):
            return {"config": "cfg.yaml"}

    env.setattr_module(reg_mod, "Registry", _FakeRegistry)
    env.setattr_module(
        cfg_mod,
        "load_config",
        lambda _path: SimpleNamespace(expanded_workdir=str(ws)),
    )
    return _sdk_common.build_sdk_options("telegrammer-agent")


def test_build_sdk_options_child_argv_carries_no_secret(_built_options) -> None:
    """THE regression guard: remove the wiring and this goes red."""
    # Arrange
    configured = _built_options.mcp_servers
    # Act
    argv = _sdk_argv(configured)
    # Assert
    assert not any(SENTINEL in arg for arg in argv)


def test_build_sdk_options_still_delivers_the_server_entry(_built_options) -> None:
    # Arrange
    configured = _built_options.mcp_servers
    # Act
    servers = read_mcp_servers(configured)
    # Assert
    assert servers["claude-code-telegrammer"]["env"][SENTINEL_KEY] == SENTINEL


def test_build_sdk_options_config_file_is_owner_only(_built_options) -> None:
    # Arrange
    path = str(_built_options.mcp_servers)
    # Act
    mode = stat.S_IMODE(os.stat(path).st_mode)
    # Assert
    assert mode == MCP_CONFIG_FILE_MODE
