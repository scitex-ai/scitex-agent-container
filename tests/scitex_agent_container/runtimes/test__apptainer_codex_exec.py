"""Tests for ``runtimes/_apptainer_codex_exec`` — the in-container codex shim.

Only the pure half is exercised here (the MCP translation and the binary
resolution); ``execv`` replaces the process and is not called by a test.
"""

from __future__ import annotations

from scitex_agent_container.runtimes._apptainer_codex_exec import (
    CODEX_BIN_ENV,
    mcp_overrides,
    resolve_codex_binary,
)


def _flags(documents: list[object]) -> dict[str, str]:
    out: dict[str, str] = {}
    flags = mcp_overrides(documents)
    for i, a in enumerate(flags):
        if a == "-c" and i + 1 < len(flags):
            k, _, v = flags[i + 1].partition("=")
            out[k] = v
    return out


def test_stdio_server_command_is_translated():
    # Arrange -- Claude's .mcp.json shape.
    document = {
        "mcpServers": {"scitex-cards": {"command": "scitex-cards", "args": ["mcp"]}}
    }
    # Act
    seen = _flags([document])
    # Assert
    assert seen["mcp_servers.scitex-cards.command"] == '"scitex-cards"'


def test_stdio_server_args_become_a_toml_array():
    # Arrange
    document = {"mcpServers": {"sac": {"command": "sac", "args": ["mcp", "serve"]}}}
    # Act
    seen = _flags([document])
    # Assert
    assert seen["mcp_servers.sac.args"] == '["mcp", "serve"]'


def test_stdio_server_env_becomes_a_toml_inline_table():
    # Arrange
    document = {"mcpServers": {"sac": {"command": "sac", "env": {"SAC_NAME": "hm"}}}}
    # Act
    seen = _flags([document])
    # Assert
    assert seen["mcp_servers.sac.env"] == '{ "SAC_NAME" = "hm" }'


def test_http_server_url_is_translated():
    # Arrange -- the streamable-HTTP shape the channel subscriber may use.
    document = {
        "mcpServers": {"bus": {"type": "http", "url": "http://127.0.0.1:19001/mcp"}}
    }
    # Act
    seen = _flags([document])
    # Assert
    assert seen["mcp_servers.bus.url"] == '"http://127.0.0.1:19001/mcp"'


def test_bare_server_map_without_the_wrapper_key_is_accepted():
    # Arrange -- the inline channel JSON may omit the mcpServers wrapper.
    document = {"channel": {"command": "sac", "args": ["mcp", "channel"]}}
    # Act
    seen = _flags([document])
    # Assert
    assert seen["mcp_servers.channel.command"] == '"sac"'


def test_documents_are_merged_in_order():
    # Arrange -- the workspace file and the inline JSON both contribute.
    first = {"mcpServers": {"one": {"command": "one"}}}
    second = {"two": {"command": "two"}}
    # Act
    seen = _flags([first, second])
    # Assert
    assert sorted(k.split(".")[1] for k in seen) == ["one", "two"]


def test_an_operator_set_binary_path_wins(env_save_restore):
    # Arrange
    env_save_restore.set(CODEX_BIN_ENV, "/opt/elsewhere/codex")
    # Act
    binary = resolve_codex_binary()
    # Assert
    assert binary == "/opt/elsewhere/codex"


def test_the_bundled_binary_is_the_default(env_save_restore):
    # Arrange -- the image venv ships openai-codex-cli-bin; nothing overrides it.
    env_save_restore.delete(CODEX_BIN_ENV)
    # Act
    binary = resolve_codex_binary()
    # Assert
    assert binary.endswith("/codex")
