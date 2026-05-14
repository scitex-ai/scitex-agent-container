"""Tests for runtimes.mcp_config — pure-function .mcp.json builder."""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes.mcp_config import (
    _setup_mcp_from_servers,
    cleanup_mcp_config,
    setup_mcp_config,
)


def _make_config(name: str = "agent-x", mcp_servers: dict | None = None) -> AgentConfig:
    return AgentConfig(name=name, mcp_servers=mcp_servers or {})


def test_setup_mcp_no_servers_is_noop(tmp_path: Path) -> None:
    cfg = _make_config(mcp_servers={})
    setup_mcp_config(cfg, str(tmp_path))
    assert not (tmp_path / ".mcp.json").exists()


def test_setup_mcp_writes_basic_server(tmp_path: Path) -> None:
    cfg = _make_config(
        mcp_servers={"hello": {"command": "node", "args": ["/x/server.js"]}}
    )
    setup_mcp_config(cfg, str(tmp_path))
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert data["mcpServers"]["hello"]["command"] == "node"
    assert data["mcpServers"]["hello"]["args"] == ["/x/server.js"]


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


def test_setup_mcp_env_var_interpolation(tmp_path: Path) -> None:
    restore = _set_env_save("MY_TOKEN", "secret-abc")
    try:
        cfg = _make_config(
            mcp_servers={
                "svc": {
                    "command": "x",
                    "env": {"TOKEN": "${MY_TOKEN}", "RAW": "literal"},
                }
            }
        )
        setup_mcp_config(cfg, str(tmp_path))
        data = json.loads((tmp_path / ".mcp.json").read_text())
        assert data["mcpServers"]["svc"]["env"]["TOKEN"] == "secret-abc"
        assert data["mcpServers"]["svc"]["env"]["RAW"] == "literal"
    finally:
        restore()


def test_setup_mcp_env_unresolved_var_kept_literal(tmp_path: Path) -> None:
    restore = _set_env_save("NOT_SET_VAR", None)
    try:
        cfg = _make_config(
            mcp_servers={"s": {"command": "x", "env": {"K": "${NOT_SET_VAR}"}}}
        )
        setup_mcp_config(cfg, str(tmp_path))
        data = json.loads((tmp_path / ".mcp.json").read_text())
        assert data["mcpServers"]["s"]["env"]["K"] == "${NOT_SET_VAR}"
    finally:
        restore()


def test_setup_mcp_expands_tilde_in_args(tmp_path: Path) -> None:
    restore = _set_env_save("HOME", str(tmp_path))
    try:
        cfg = _make_config(
            mcp_servers={"s": {"command": "x", "args": ["~/conf.json", "/abs/path"]}}
        )
        setup_mcp_config(cfg, str(tmp_path))
        data = json.loads((tmp_path / ".mcp.json").read_text())
        args = data["mcpServers"]["s"]["args"]
        assert args[0] == str(tmp_path / "conf.json")
        assert args[1] == "/abs/path"
    finally:
        restore()


def test_setup_mcp_merges_with_existing(tmp_path: Path) -> None:
    pre = {"mcpServers": {"old": {"command": "y"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(pre))
    cfg = _make_config(mcp_servers={"new": {"command": "x"}})
    setup_mcp_config(cfg, str(tmp_path))
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert set(data["mcpServers"]) == {"old", "new"}


def test_setup_mcp_recovers_from_malformed_existing(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("not-json{[")
    cfg = _make_config(mcp_servers={"s": {"command": "x"}})
    setup_mcp_config(cfg, str(tmp_path))
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert "s" in data["mcpServers"]


def test_setup_mcp_recovers_from_non_dict_existing(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("[1,2,3]")
    cfg = _make_config(mcp_servers={"s": {"command": "x"}})
    setup_mcp_config(cfg, str(tmp_path))
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert data["mcpServers"] == {"s": {"command": "x"}}


def test_setup_mcp_creates_parent_dirs(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c"
    cfg = _make_config(mcp_servers={"s": {"command": "x"}})
    setup_mcp_config(cfg, str(deep))
    assert (deep / ".mcp.json").exists()


def test_cleanup_mcp_no_servers_is_noop(tmp_path: Path) -> None:
    cfg = _make_config(mcp_servers={})
    cleanup_mcp_config(cfg, str(tmp_path))  # must not raise


def test_cleanup_mcp_missing_file_is_noop(tmp_path: Path) -> None:
    cfg = _make_config(mcp_servers={"s": {"command": "x"}})
    cleanup_mcp_config(cfg, str(tmp_path))  # must not raise


def test_cleanup_mcp_removes_only_owned_entries(tmp_path: Path) -> None:
    data = {"mcpServers": {"keep": {"command": "k"}, "drop": {"command": "d"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(data))
    cfg = _make_config(mcp_servers={"drop": {"command": "d"}})
    cleanup_mcp_config(cfg, str(tmp_path))
    remaining = json.loads((tmp_path / ".mcp.json").read_text())
    assert remaining["mcpServers"] == {"keep": {"command": "k"}}


def test_cleanup_mcp_deletes_file_when_empty(tmp_path: Path) -> None:
    data = {"mcpServers": {"only": {"command": "x"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(data))
    cfg = _make_config(mcp_servers={"only": {"command": "x"}})
    cleanup_mcp_config(cfg, str(tmp_path))
    assert not (tmp_path / ".mcp.json").exists()


def test_cleanup_mcp_no_matching_keys_noop(tmp_path: Path) -> None:
    data = {"mcpServers": {"other": {"command": "o"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(data))
    cfg = _make_config(mcp_servers={"missing": {"command": "x"}})
    cleanup_mcp_config(cfg, str(tmp_path))
    # File untouched
    assert json.loads((tmp_path / ".mcp.json").read_text()) == data


def test_cleanup_mcp_malformed_json_is_silent(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("{not json")
    cfg = _make_config(mcp_servers={"x": {"command": "x"}})
    cleanup_mcp_config(cfg, str(tmp_path))  # must not raise


def test_setup_mcp_from_servers_direct_empty_is_noop(tmp_path: Path) -> None:
    _setup_mcp_from_servers({}, str(tmp_path), "an-agent")
    assert not (tmp_path / ".mcp.json").exists()
