"""Tests for runtimes.mcp_config — pure-function .mcp.json builder."""

from __future__ import annotations

import hashlib
import json
from functools import reduce
from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes.mcp_config import (
    _setup_mcp_from_servers,
    cleanup_mcp_config,
    setup_mcp_config,
)


def _make_config(name: str = "agent-x", mcp_servers: dict | None = None) -> AgentConfig:
    return AgentConfig(name=name, mcp_servers=mcp_servers or {})


def _set_env_save(key, val):
    """PA-306: explicit env mutator with restore-function returned."""
    import os

    saved = os.environ.get(key)
    if val is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = val

    def _restore():
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved

    return _restore


def test_setup_mcp_no_servers_is_noop(tmp_path: Path) -> None:
    # Arrange
    cfg = _make_config(mcp_servers={})
    # Act
    setup_mcp_config(cfg, str(tmp_path))
    # Assert
    assert not (tmp_path / ".mcp.json").exists()


def test_setup_mcp_writes_basic_server_command(tmp_path: Path) -> None:
    # Arrange
    cfg = _make_config(
        mcp_servers={"hello": {"command": "node", "args": ["/x/server.js"]}}
    )
    # Act
    setup_mcp_config(cfg, str(tmp_path))
    # Assert
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert data["mcpServers"]["hello"]["command"] == "node"


def test_setup_mcp_writes_basic_server_args(tmp_path: Path) -> None:
    # Arrange
    cfg = _make_config(
        mcp_servers={"hello": {"command": "node", "args": ["/x/server.js"]}}
    )
    # Act
    setup_mcp_config(cfg, str(tmp_path))
    # Assert
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert data["mcpServers"]["hello"]["args"] == ["/x/server.js"]


def test_setup_mcp_interpolates_set_env_var(tmp_path: Path) -> None:
    # Arrange
    restore = _set_env_save("MY_TOKEN", "secret-abc")
    cfg = _make_config(
        mcp_servers={
            "svc": {
                "command": "x",
                "env": {"TOKEN": "${MY_TOKEN}", "RAW": "literal"},
            }
        }
    )
    try:
        # Act
        setup_mcp_config(cfg, str(tmp_path))
        data = json.loads((tmp_path / ".mcp.json").read_text())
        # Assert
        assert data["mcpServers"]["svc"]["env"]["TOKEN"] == "secret-abc"
    finally:
        restore()


def test_setup_mcp_keeps_literal_env_value_unchanged(tmp_path: Path) -> None:
    # Arrange
    restore = _set_env_save("MY_TOKEN", "secret-abc")
    cfg = _make_config(
        mcp_servers={
            "svc": {
                "command": "x",
                "env": {"TOKEN": "${MY_TOKEN}", "RAW": "literal"},
            }
        }
    )
    try:
        # Act
        setup_mcp_config(cfg, str(tmp_path))
        data = json.loads((tmp_path / ".mcp.json").read_text())
        # Assert
        assert data["mcpServers"]["svc"]["env"]["RAW"] == "literal"
    finally:
        restore()


def test_setup_mcp_env_unresolved_var_kept_literal(tmp_path: Path) -> None:
    # Arrange
    restore = _set_env_save("NOT_SET_VAR", None)
    cfg = _make_config(
        mcp_servers={"s": {"command": "x", "env": {"K": "${NOT_SET_VAR}"}}}
    )
    try:
        # Act
        setup_mcp_config(cfg, str(tmp_path))
        data = json.loads((tmp_path / ".mcp.json").read_text())
        # Assert
        assert data["mcpServers"]["s"]["env"]["K"] == "${NOT_SET_VAR}"
    finally:
        restore()


def test_setup_mcp_expands_tilde_in_arg(tmp_path: Path) -> None:
    # Arrange
    restore = _set_env_save("HOME", str(tmp_path))
    cfg = _make_config(
        mcp_servers={"s": {"command": "x", "args": ["~/conf.json", "/abs/path"]}}
    )
    try:
        # Act
        setup_mcp_config(cfg, str(tmp_path))
        data = json.loads((tmp_path / ".mcp.json").read_text())
        # Assert
        assert data["mcpServers"]["s"]["args"][0] == str(tmp_path / "conf.json")
    finally:
        restore()


def test_setup_mcp_leaves_absolute_path_arg_unchanged(tmp_path: Path) -> None:
    # Arrange
    restore = _set_env_save("HOME", str(tmp_path))
    cfg = _make_config(
        mcp_servers={"s": {"command": "x", "args": ["~/conf.json", "/abs/path"]}}
    )
    try:
        # Act
        setup_mcp_config(cfg, str(tmp_path))
        data = json.loads((tmp_path / ".mcp.json").read_text())
        # Assert
        assert data["mcpServers"]["s"]["args"][1] == "/abs/path"
    finally:
        restore()


def test_setup_mcp_merges_with_existing_servers(tmp_path: Path) -> None:
    # Arrange
    pre = {"mcpServers": {"old": {"command": "y"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(pre))
    cfg = _make_config(mcp_servers={"new": {"command": "x"}})
    # Act
    setup_mcp_config(cfg, str(tmp_path))
    # Assert
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert set(data["mcpServers"]) == {"old", "new"}


@pytest.fixture
def foreign_cct_environment():
    restore_identity = _set_env_save("CCT_AGENT_ID", "paper-scitex-clew")
    restore_token = _set_env_save("CCT_BOT_TOKEN", None)
    try:
        yield
    finally:
        restore_token()
        restore_identity()


@pytest.fixture
def pinned_cct(tmp_path, foreign_cct_environment):
    # Arrange
    cfg = _make_config(
        name="scitex-research-lead",
        mcp_servers={
            "claude-code-telegrammer": {
                "command": "bun",
                "args": ["telegram-server.ts"],
                "env": {
                    "CCT_AGENT_ID": "${CCT_AGENT_ID}",
                    "CCT_BOT_TOKEN": "${CCT_BOT_TOKEN}",
                    "CLAUDE_CODE_TELEGRAMMER_EXTERNAL_POLLER": "1",
                },
            },
            "other": {"command": "other", "env": {"CCT_AGENT_ID": "other-id"}},
        },
    )
    cfg.env = {"CCT_AGENT_ID": "stale-spec-id"}

    setup_mcp_config(cfg, str(tmp_path))

    servers = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]
    return cfg, servers


@pytest.mark.parametrize(
    "path,expected",
    [
        (("claude-code-telegrammer", "env", "CCT_AGENT_ID"), "scitex-research-lead"),
        (("claude-code-telegrammer", "env", "CCT_BOT_TOKEN"), "${CCT_BOT_TOKEN}"),
        (
            (
                "claude-code-telegrammer",
                "env",
                "CLAUDE_CODE_TELEGRAMMER_EXTERNAL_POLLER",
            ),
            "1",
        ),
        (("other", "env", "CCT_AGENT_ID"), "other-id"),
    ],
)
def test_setup_mcp_pins_cct_identity_over_project_and_spec_env(
    pinned_cct, path, expected
):
    # Arrange
    _, servers = pinned_cct
    # Act
    value = reduce(lambda node, key: node[key], path, servers)
    # Assert
    assert value == expected


def test_setup_mcp_does_not_mutate_declared_identity_template(pinned_cct):
    # Arrange
    config, _ = pinned_cct
    # Act
    value = config.mcp_servers["claude-code-telegrammer"]["env"]["CCT_AGENT_ID"]
    # Assert
    assert value == "${CCT_AGENT_ID}"


@pytest.mark.parametrize("server", ["claude-code-telegrammer", "scitex-cards"])
def test_setup_mcp_pins_baseline_cct_without_spec_servers(
    tmp_path: Path, server
) -> None:
    # Arrange
    baseline = {
        "mcpServers": {
            "claude-code-telegrammer": {
                "command": "bun",
                "env": {"CCT_AGENT_ID": "old-project"},
            },
            "scitex-cards": {"command": "scitex-cards", "env": {"KEEP": "existing"}},
        },
    }
    (tmp_path / ".mcp.json").write_text(json.dumps(baseline))
    cfg = _make_config(name="scitex-infrastructure-lead")
    cfg.env = {"UNRELATED": "new-value"}
    expected = {
        "claude-code-telegrammer": {
            "command": "bun",
            "env": {"CCT_AGENT_ID": cfg.name},
        },
        "scitex-cards": baseline["mcpServers"]["scitex-cards"],
    }

    setup_mcp_config(cfg, str(tmp_path))
    setup_mcp_config(cfg, str(tmp_path))

    # Act
    servers = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]
    # Assert
    assert servers[server] == expected[server]


@pytest.mark.parametrize(
    "path,expected",
    [
        (("env", "CCT_AGENT_ID"), "scitex-research-lead"),
        (("env_vars",), ["CCT_AGENT_ID"]),
    ],
)
def test_generated_cct_identity_survives_codex_env_forwarding(
    tmp_path: Path, foreign_cct_environment, path, expected
) -> None:
    # Arrange
    import tomllib

    from scitex_agent_container.runtimes._apptainer_codex_exec import mcp_overrides

    cfg = _make_config(
        name="scitex-research-lead",
        mcp_servers={
            "claude-code-telegrammer": {
                "command": "bun",
                "env": {"CCT_AGENT_ID": "${CCT_AGENT_ID}"},
            },
        },
    )
    setup_mcp_config(cfg, str(tmp_path))
    document = json.loads((tmp_path / ".mcp.json").read_text())

    flags = mcp_overrides([document], environ={"CCT_AGENT_ID": "paper-scitex-clew"})
    # Act
    effective = tomllib.loads("\n".join(flags[1::2]))["mcp_servers"][
        "claude-code-telegrammer"
    ]

    # Assert
    assert reduce(lambda node, key: node[key], path, effective) == expected


def test_setup_mcp_recovers_from_malformed_existing_file(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / ".mcp.json").write_text("not-json{[")
    cfg = _make_config(mcp_servers={"s": {"command": "x"}})
    # Act
    setup_mcp_config(cfg, str(tmp_path))
    # Assert
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert "s" in data["mcpServers"]


def test_setup_mcp_recovers_from_non_dict_existing_file(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / ".mcp.json").write_text("[1,2,3]")
    cfg = _make_config(mcp_servers={"s": {"command": "x"}})
    # Act
    setup_mcp_config(cfg, str(tmp_path))
    # Assert
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert data["mcpServers"] == {"s": {"command": "x"}}


def test_setup_mcp_creates_missing_parent_dirs(tmp_path: Path) -> None:
    # Arrange
    deep = tmp_path / "a" / "b" / "c"
    cfg = _make_config(mcp_servers={"s": {"command": "x"}})
    # Act
    setup_mcp_config(cfg, str(deep))
    # Assert
    assert (deep / ".mcp.json").exists()


def test_cleanup_mcp_no_servers_does_not_raise(tmp_path: Path) -> None:
    # Arrange
    cfg = _make_config(mcp_servers={})
    # Act
    cleanup_mcp_config(cfg, str(tmp_path))
    # Assert
    assert not (tmp_path / ".mcp.json").exists()


def test_cleanup_mcp_missing_file_does_not_raise(tmp_path: Path) -> None:
    # Arrange
    cfg = _make_config(mcp_servers={"s": {"command": "x"}})
    # Act
    cleanup_mcp_config(cfg, str(tmp_path))
    # Assert
    assert not (tmp_path / ".mcp.json").exists()


def test_cleanup_mcp_removes_only_owned_entries(tmp_path: Path) -> None:
    # Arrange
    data = {"mcpServers": {"keep": {"command": "k"}, "drop": {"command": "d"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(data))
    cfg = _make_config(mcp_servers={"drop": {"command": "d"}})
    # Act
    cleanup_mcp_config(cfg, str(tmp_path))
    # Assert
    remaining = json.loads((tmp_path / ".mcp.json").read_text())
    assert remaining["mcpServers"] == {"keep": {"command": "k"}}


def test_cleanup_mcp_deletes_file_when_no_servers_remain(tmp_path: Path) -> None:
    # Arrange
    data = {"mcpServers": {"only": {"command": "x"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(data))
    cfg = _make_config(mcp_servers={"only": {"command": "x"}})
    # Act
    cleanup_mcp_config(cfg, str(tmp_path))
    # Assert
    assert not (tmp_path / ".mcp.json").exists()


def test_cleanup_mcp_no_matching_keys_leaves_file_untouched(tmp_path: Path) -> None:
    # Arrange
    data = {"mcpServers": {"other": {"command": "o"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(data))
    cfg = _make_config(mcp_servers={"missing": {"command": "x"}})
    # Act
    cleanup_mcp_config(cfg, str(tmp_path))
    # Assert
    assert json.loads((tmp_path / ".mcp.json").read_text()) == data


def test_cleanup_mcp_malformed_json_is_silent(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / ".mcp.json").write_text("{not json")
    cfg = _make_config(mcp_servers={"x": {"command": "x"}})
    # Act
    cleanup_mcp_config(cfg, str(tmp_path))
    # Assert — file still exists (silent failure, not a raise)
    assert (tmp_path / ".mcp.json").exists()


def test_setup_mcp_from_servers_direct_empty_is_noop(tmp_path: Path) -> None:
    # Arrange
    servers: dict = {}
    # Act
    _setup_mcp_from_servers(servers, str(tmp_path), "an-agent")
    # Assert
    assert not (tmp_path / ".mcp.json").exists()


# --- per-entry inherit_env (spec.mcp_servers.<name>.inherit_env) -----------

_DSN = "postgresql://fixture.invalid:65432/fixture_db"
_INHERIT_ENV = {"SCITEX_STORE_DSN": _DSN, "PGUSER": "fixture__x", "FOO": "bar"}
#: sha256 of the ``.mcp.json`` origin/develop (before ``inherit_env``) wrote
#: for ``_inherit_fixture()`` — the default must stay byte-identical to it.
_DEVELOP_SHA256 = "d2ba83b5a3b1c0f65a836c4eea1961be844a5c54bbfb6988bafa4090ca5fb659"


def _inherit_fixture(**browser_extra) -> AgentConfig:
    cards = {"command": "scitex-cards", "args": ["mcp", "start"], "alwaysLoad": True}
    browser = {"command": "browser-mcp", "env": {"X": "1"}, "alwaysLoad": True}
    remote = {"type": "http", "url": "http://example.invalid/mcp"}
    servers = {"scitex-cards": cards, "browser": {**browser, **browser_extra}}
    return AgentConfig(
        name="fixture-agent",
        env=dict(_INHERIT_ENV),
        mcp_servers={**servers, "remote": remote},
    )


def _written(tmp_path: Path, **browser_extra) -> str:
    setup_mcp_config(_inherit_fixture(**browser_extra), str(tmp_path))
    return (tmp_path / ".mcp.json").read_text()


def test_default_mcp_json_is_byte_identical_to_develop(tmp_path: Path) -> None:
    # Arrange
    extra: dict = {}
    # Act
    text = _written(tmp_path, **extra)
    # Assert
    assert hashlib.sha256(text.encode()).hexdigest() == _DEVELOP_SHA256


def test_explicit_inherit_env_true_matches_develop_bytes(tmp_path: Path) -> None:
    # Arrange
    extra = {"inherit_env": True}
    # Act
    text = _written(tmp_path, **extra)
    # Assert
    assert hashlib.sha256(text.encode()).hexdigest() == _DEVELOP_SHA256


def test_inherit_env_false_entry_gets_only_declared_env(tmp_path: Path) -> None:
    # Arrange
    extra = {"inherit_env": False}
    # Act
    data = json.loads(_written(tmp_path, **extra))
    # Assert
    assert data["mcpServers"]["browser"]["env"] == {"X": "1"}


def test_inherit_env_list_entry_gets_only_listed_names(tmp_path: Path) -> None:
    # Arrange
    extra = {"inherit_env": ["FOO"]}
    # Act
    data = json.loads(_written(tmp_path, **extra))
    # Assert
    assert data["mcpServers"]["browser"]["env"] == {"X": "1", "FOO": "bar"}


def test_inherit_env_false_leaves_other_entries_inheriting(tmp_path: Path) -> None:
    # Arrange
    extra = {"inherit_env": False}
    # Act
    data = json.loads(_written(tmp_path, **extra))
    # Assert
    assert data["mcpServers"]["scitex-cards"]["env"] == _INHERIT_ENV


def test_inherit_env_key_is_not_written_to_mcp_json(tmp_path: Path) -> None:
    # Arrange
    extra = {"inherit_env": ["FOO"]}
    # Act
    text = _written(tmp_path, **extra)
    # Assert
    assert "inherit_env" not in text


def test_malformed_inherit_env_refuses_to_write(tmp_path: Path) -> None:
    # Arrange
    extra = {"inherit_env": "no"}
    # Act
    ctx = pytest.raises(ValueError, match="inherit_env")
    # Assert
    with ctx:
        _written(tmp_path, **extra)
