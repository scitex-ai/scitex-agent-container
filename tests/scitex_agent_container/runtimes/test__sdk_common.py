"""Tests for ``runtimes/_sdk_common.py``.

Covers the three concerns the helper consolidates:

  * auth-path selection (env / credentials_file / bridged_oauth /
    bridged_api_key / failure)
  * workspace + MCP-server resolution from the agent registry
  * ``ClaudeAgentOptions`` composition

The SDK itself is patched out — the option-builder test asserts the
kwargs we *would* pass; we don't need a live ``ClaudeAgentOptions``
roundtrip here because the live probe in the design doc already
covered that surface.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scitex_agent_container.runtimes import _sdk_common
from scitex_agent_container.runtimes._sdk_common import (
    SDKCommonError,
    build_sdk_options,
    provision_anthropic_auth,
    resolve_agent_workspace,
)

_OAUTH = _sdk_common._OAUTH_ENV
_APIKEY = _sdk_common._APIKEY_ENV


# ---------------------------------------------------------------------------
# provision_anthropic_auth
# ---------------------------------------------------------------------------


class TestProvisionAuth:
    def test_env_var_already_set_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-existing")
        # Even if a credentials file is around, env wins.
        monkeypatch.setattr(_sdk_common, "_CRED_FILE", tmp_path / ".credentials.json")
        assert provision_anthropic_auth() == "env"

    def test_credentials_file_skips_env_bridge(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cred = tmp_path / ".credentials.json"
        cred.write_text('{"claudeAiOauth": {"accessToken": "tok"}}')
        monkeypatch.setattr(_sdk_common, "_CRED_FILE", cred)
        # Even with bridge envs set, cred file wins so SDK can use OAuth.
        monkeypatch.setenv(_OAUTH, "sk-ant-oat-bridge")
        assert provision_anthropic_auth() == "credentials_file"
        # Critical: env was NOT mutated, OAuth path stays clean.
        import os

        assert "ANTHROPIC_API_KEY" not in os.environ

    def test_bridge_oauth_when_no_credentials_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(_sdk_common, "_CRED_FILE", tmp_path / "missing")
        monkeypatch.setenv(_OAUTH, "sk-ant-oat-bridge")
        assert provision_anthropic_auth() == "bridged_oauth"
        import os

        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-oat-bridge"

    def test_bridge_api_key_only_when_oauth_absent(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv(_OAUTH, raising=False)
        monkeypatch.setattr(_sdk_common, "_CRED_FILE", tmp_path / "missing")
        monkeypatch.setenv(_APIKEY, "sk-ant-api-bridge")
        assert provision_anthropic_auth() == "bridged_api_key"

    def test_oauth_preferred_over_api_key(self, monkeypatch, tmp_path):
        """If both bridge envs are set, OAuth (flat-rate) wins."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(_sdk_common, "_CRED_FILE", tmp_path / "missing")
        monkeypatch.setenv(_OAUTH, "sk-ant-oat-bridge")
        monkeypatch.setenv(_APIKEY, "sk-ant-api-bridge")
        assert provision_anthropic_auth() == "bridged_oauth"
        import os

        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-oat-bridge"

    def test_no_auth_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv(_OAUTH, raising=False)
        monkeypatch.delenv(_APIKEY, raising=False)
        monkeypatch.setattr(_sdk_common, "_CRED_FILE", tmp_path / "missing")
        with pytest.raises(SDKCommonError):
            provision_anthropic_auth()


# ---------------------------------------------------------------------------
# resolve_agent_workspace
# ---------------------------------------------------------------------------


class _FakeRegistry:
    def __init__(self, entry):
        self._entry = entry

    def get(self, _name):
        return self._entry


class TestResolveWorkspace:
    def test_unknown_agent_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            "scitex_agent_container._state.registry.Registry",
            lambda: _FakeRegistry(None),
        )
        assert resolve_agent_workspace("nope") == ({}, None)

    def test_workspace_without_mcp_json(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setattr(
            "scitex_agent_container._state.registry.Registry",
            lambda: _FakeRegistry({"config": "cfg.yaml"}),
        )
        monkeypatch.setattr(
            "scitex_agent_container.config.load_config",
            lambda _path: SimpleNamespace(expanded_workdir=str(ws)),
        )
        assert resolve_agent_workspace("alpha") == ({}, str(ws))

    def test_mcp_servers_parsed_with_env_substitution(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "stx": {
                            "command": "scitex",
                            "args": ["mcp", "--token", "${MY_TOKEN}"],
                        }
                    }
                }
            )
        )
        monkeypatch.setenv("MY_TOKEN", "tk-123")
        monkeypatch.setattr(
            "scitex_agent_container._state.registry.Registry",
            lambda: _FakeRegistry({"config": "cfg.yaml"}),
        )
        monkeypatch.setattr(
            "scitex_agent_container.config.load_config",
            lambda _path: SimpleNamespace(expanded_workdir=str(ws)),
        )
        servers, cwd = resolve_agent_workspace("alpha")
        assert cwd == str(ws)
        assert servers == {
            "stx": {
                "command": "scitex",
                "args": ["mcp", "--token", "tk-123"],
                "type": "stdio",
            }
        }

    def test_unresolved_env_ref_passes_through_literal(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"x": {"args": ["${UNSET_VAR}"]}}})
        )
        monkeypatch.delenv("UNSET_VAR", raising=False)
        monkeypatch.setattr(
            "scitex_agent_container._state.registry.Registry",
            lambda: _FakeRegistry({"config": "cfg.yaml"}),
        )
        monkeypatch.setattr(
            "scitex_agent_container.config.load_config",
            lambda _path: SimpleNamespace(expanded_workdir=str(ws)),
        )
        servers, _ = resolve_agent_workspace("alpha")
        assert servers["x"]["args"] == ["${UNSET_VAR}"]

    def test_malformed_mcp_json_tolerated(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".mcp.json").write_text("{not valid json")
        monkeypatch.setattr(
            "scitex_agent_container._state.registry.Registry",
            lambda: _FakeRegistry({"config": "cfg.yaml"}),
        )
        monkeypatch.setattr(
            "scitex_agent_container.config.load_config",
            lambda _path: SimpleNamespace(expanded_workdir=str(ws)),
        )
        servers, cwd = resolve_agent_workspace("alpha")
        assert servers == {}
        assert cwd == str(ws)


# ---------------------------------------------------------------------------
# build_sdk_options
# ---------------------------------------------------------------------------


class TestBuildOptions:
    def test_composes_all_layers(self, monkeypatch, tmp_path):
        # Auth: pretend cred file exists so no env mutation.
        cred = tmp_path / ".credentials.json"
        cred.write_text("{}")
        monkeypatch.setattr(_sdk_common, "_CRED_FILE", cred)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # Workspace: register a fake agent.
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"stx": {"command": "scitex"}}})
        )
        monkeypatch.setattr(
            "scitex_agent_container._state.registry.Registry",
            lambda: _FakeRegistry({"config": "cfg.yaml"}),
        )
        monkeypatch.setattr(
            "scitex_agent_container.config.load_config",
            lambda _path: SimpleNamespace(expanded_workdir=str(ws)),
        )

        opts = build_sdk_options(
            "alpha",
            system_prompt="be brief",
            model="claude-haiku-4-5",
            permission_mode="bypassPermissions",
        )

        assert opts.system_prompt == "be brief"
        assert opts.model == "claude-haiku-4-5"
        assert opts.permission_mode == "bypassPermissions"
        assert str(opts.cwd) == str(ws)
        assert "stx" in opts.mcp_servers  # type: ignore[operator]

    def test_extra_kwargs_pass_through(self, monkeypatch, tmp_path):
        cred = tmp_path / ".credentials.json"
        cred.write_text("{}")
        monkeypatch.setattr(_sdk_common, "_CRED_FILE", cred)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(
            "scitex_agent_container._state.registry.Registry",
            lambda: _FakeRegistry(None),
        )
        opts = build_sdk_options("nope", extra={"continue_conversation": True})
        assert opts.continue_conversation is True

    def test_missing_sdk_raises(self, monkeypatch):
        # Simulate the SDK not being installed.
        import builtins

        real_import = builtins.__import__

        def _fake_import(name, *a, **kw):
            if name == "claude_agent_sdk":
                raise ImportError("simulated absence")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        with pytest.raises(SDKCommonError, match="claude-agent-sdk is not installed"):
            build_sdk_options("any")
