"""Tests for ``runtimes/_apptainer_codex_exec`` — the in-container codex shim.

Only the pure half is exercised here (the MCP translation and the binary
resolution); ``execv`` replaces the process and is not called by a test.
"""

from __future__ import annotations

import json

from scitex_agent_container.runtimes._apptainer_codex_exec import (
    CODEX_BIN_ENV,
    adapt_hook_commands,
    mcp_overrides,
    resolve_codex_binary,
    split_env_placeholders,
    write_hooks_from,
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


def _settings(tmp_path, document: dict):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_hooks_block_is_copied_into_codex_home(tmp_path):
    # Arrange -- the fleet's shape: event -> matcher -> command hooks.
    block = {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
        ]
    }
    settings = _settings(tmp_path, {"hooks": block, "other": 1})
    home = tmp_path / "codex-home"
    # Act
    written = write_hooks_from(str(settings), str(home))
    # Assert
    assert json.loads(written.read_text()) == {"hooks": block}


def test_settings_without_hooks_write_nothing(tmp_path):
    # Arrange
    settings = _settings(tmp_path, {"permissions": {}})
    home = tmp_path / "codex-home"
    # Act
    written = write_hooks_from(str(settings), str(home))
    # Assert
    assert written is None


def test_absent_settings_write_nothing(tmp_path):
    # Arrange
    home = tmp_path / "codex-home"
    # Act
    written = write_hooks_from(str(tmp_path / "missing.json"), str(home))
    # Assert
    assert written is None


def test_hooks_file_is_rewritten_from_the_settings_each_boot(tmp_path):
    # Arrange -- a stale hooks.json from a previous boot must not win.
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "hooks.json").write_text('{"hooks": {"Stop": []}}', encoding="utf-8")
    settings = _settings(tmp_path, {"hooks": {"PreToolUse": []}})
    # Act
    written = write_hooks_from(str(settings), str(home))
    # Assert
    assert json.loads(written.read_text()) == {"hooks": {"PreToolUse": []}}


def test_whole_placeholder_env_values_are_forwarded_by_name():
    # Arrange -- the telegrammer's shape: every value is ${NAME}.
    env = {"CCT_AGENT_ID": "${CCT_AGENT_ID}", "CCT_BOT_TOKEN": "${CCT_BOT_TOKEN}"}
    # Act
    literal, forwarded = split_env_placeholders(env)
    # Assert -- names only; no value ever reaches the argv.
    assert (literal, forwarded) == ({}, ["CCT_AGENT_ID", "CCT_BOT_TOKEN"])


def test_plain_env_values_stay_literal():
    # Arrange
    env = {"SCITEX_TODO_CHANNEL_SOURCE": "cards"}
    # Act
    literal, forwarded = split_env_placeholders(env)
    # Assert
    assert (literal, forwarded) == ({"SCITEX_TODO_CHANNEL_SOURCE": "cards"}, [])


def test_embedded_placeholders_expand_from_the_environment(env_save_restore):
    # Arrange -- a placeholder inside more text cannot be forwarded by name.
    env_save_restore.set("SAC_TEST_HOST", "compute-04")
    env = {"URL": "http://${SAC_TEST_HOST}:19001/mcp"}
    # Act
    literal, _ = split_env_placeholders(env)
    # Assert
    assert literal == {"URL": "http://compute-04:19001/mcp"}


def test_mcp_overrides_emit_env_vars_for_placeholders():
    # Arrange
    document = {
        "mcpServers": {
            "tg": {"command": "bun", "env": {"CCT_AGENT_ID": "${CCT_AGENT_ID}"}}
        }
    }
    # Act
    seen = _flags([document])
    # Assert
    assert seen["mcp_servers.tg.env_vars"] == '["CCT_AGENT_ID"]'


def test_the_rtk_hook_is_piped_through_the_output_adapter():
    # Arrange -- the one fleet hook whose stdout Codex misreads.
    hooks = {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "rtk hook claude"}],
            }
        ]
    }
    # Act
    adapted = adapt_hook_commands(hooks)
    # Assert
    assert adapted["PreToolUse"][0]["hooks"][0]["command"].startswith(
        "rtk hook claude | python3 -m "
    )


def test_other_hooks_are_copied_unchanged():
    # Arrange
    hooks = {"Stop": [{"hooks": [{"type": "command", "command": "sac never-stop"}]}]}
    # Act
    adapted = adapt_hook_commands(hooks)
    # Assert
    assert adapted == hooks
