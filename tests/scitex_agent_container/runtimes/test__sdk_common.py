"""Tests for ``runtimes/_sdk_common.py``.

Covers the three concerns the helper consolidates:

  * auth-path selection (env / credentials_file / bridged_sac / failure)
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

_SAC_KEY = _sdk_common._SAC_API_KEY_ENV


# ---------------------------------------------------------------------------
# provision_anthropic_auth
# ---------------------------------------------------------------------------


class TestProvisionAuth:
    def test_pre_set_anthropic_api_key_is_popped_when_sac_unset(
        self, monkeypatch, tmp_path
    ):
        """A pre-set ``ANTHROPIC_API_KEY`` is *never* honoured.

        See the module-level comment in ``runtimes/_sdk_common.py``
        for the why — short version: stale dotfiles exports of
        ``ANTHROPIC_API_KEY`` shadow the credentials.json path and
        produce surprise pay-per-token billing or "401 Invalid auth"
        in production.
        """
        import os

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stale-from-dotfiles")
        monkeypatch.delenv(_SAC_KEY, raising=False)
        cred = tmp_path / ".credentials.json"
        cred.write_text('{"claudeAiOauth": {"accessToken": "tok"}}')
        monkeypatch.setattr(_sdk_common, "_CRED_FILE", cred)

        assert provision_anthropic_auth() == "credentials_file"
        # The pre-set ANTHROPIC_API_KEY must have been popped so the
        # SDK auto-reader can't pick it up after we return.
        assert "ANTHROPIC_API_KEY" not in os.environ

    def test_sac_value_overrides_pre_set_anthropic_api_key(self, monkeypatch, tmp_path):
        """``SAC_ANTHROPIC_API_KEY`` is the only trusted env input — it
        overwrites a stale ``ANTHROPIC_API_KEY`` unconditionally."""
        import os

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stale-from-dotfiles")
        monkeypatch.setenv(_SAC_KEY, "sk-ant-api-sac")
        monkeypatch.setattr(_sdk_common, "_CRED_FILE", tmp_path / "missing")

        assert provision_anthropic_auth() == "sac_env"
        # Override happened: the value the SDK will see is SAC's, not the stale one.
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-api-sac"

    def test_credentials_file_wins_over_sac_env(self, monkeypatch, tmp_path):
        """Cred file beats SAC env: Pro/Max OAuth flat-rate is preferred."""
        import os

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cred = tmp_path / ".credentials.json"
        cred.write_text('{"claudeAiOauth": {"accessToken": "tok"}}')
        monkeypatch.setattr(_sdk_common, "_CRED_FILE", cred)
        monkeypatch.setenv(_SAC_KEY, "sk-ant-api-sac")

        assert provision_anthropic_auth() == "credentials_file"
        # SAC value was still mirrored to ANTHROPIC_API_KEY by the
        # override step (the SDK may pick either path; the file is
        # preferred and the env is now a known-good fallback rather
        # than a stale dotfiles value).
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-api-sac"

    def test_sac_env_when_no_credentials_file(self, monkeypatch, tmp_path):
        """SAC value (any form) is mirrored to ANTHROPIC_API_KEY when
        the cred file is absent. Sac never writes credentials.json — the
        flow is one-way (cred file → SAC → ANTHROPIC), never the reverse."""
        import os

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(_sdk_common, "_CRED_FILE", tmp_path / "missing")
        monkeypatch.setenv(_SAC_KEY, "sk-ant-api-sac")

        assert provision_anthropic_auth() == "sac_env"
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-api-sac"
        # The cred file path was never touched.
        assert not (tmp_path / "missing").exists()

    def test_oauth_value_does_not_synthesise_credentials_file(
        self, monkeypatch, tmp_path
    ):
        """``sk-ant-oat*`` SAC value flows through env override only —
        sac NEVER synthesises credentials.json. If the operator wants
        the OAuth flat-rate path inside CI, they bind-mount/copy their
        real credentials.json (with a working refresh_token); the
        synth-from-bare-token hack was rejected by Anthropic's API
        anyway and contradicted the one-way auth flow."""
        import os

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cred_target = tmp_path / ".claude" / ".credentials.json"
        monkeypatch.setattr(_sdk_common, "_CRED_FILE", cred_target)
        monkeypatch.setenv(_SAC_KEY, "sk-ant-oat-zzz")

        assert provision_anthropic_auth() == "sac_env"
        # The SAC value reaches the SDK via ANTHROPIC_API_KEY override.
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-oat-zzz"
        # Crucially, sac did NOT write the cred file.
        assert not cred_target.exists()

    def test_no_auth_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv(_SAC_KEY, raising=False)
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
