"""Tests for ``runtimes/_sdk_common.py``.

Covers the three concerns the helper consolidates:

  * auth-path selection (env / credentials_file / bridged_sac / failure)
  * workspace + MCP-server resolution from the agent registry
  * ``ClaudeAgentOptions`` composition

PA-306: no `monkeypatch`. Env vars and module attributes are saved /
restored via a ``sdk_env`` fixture that yields a small ``Env`` helper.
``Env.setattr_module(_sdk_common, '_CRED_FILE', path)`` is the
equivalent of ``monkeypatch.setattr`` with explicit teardown.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any

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
# Helper fixture (replaces monkeypatch)
# ---------------------------------------------------------------------------


class _Env:
    """Records env / attribute mutations and reverses them on teardown."""

    def __init__(self) -> None:
        self._env_snapshots: dict[str, str | None] = {}
        self._attr_snapshots: list[tuple[Any, str, Any]] = []
        self._sys_modules_keys: list[str] = []
        self._sys_modules_prev: dict[str, Any] = {}

    def setenv(self, key: str, value: str) -> None:
        if key not in self._env_snapshots:
            self._env_snapshots[key] = os.environ.get(key)
        os.environ[key] = value

    def delenv(self, key: str) -> None:
        if key not in self._env_snapshots:
            self._env_snapshots[key] = os.environ.get(key)
        os.environ.pop(key, None)

    def setattr_module(self, obj: Any, name: str, value: Any) -> None:
        # Record the FIRST seen value only (so multiple setattr in the same
        # test still restore to the truly-original).
        if not any(a is obj and n == name for a, n, _ in self._attr_snapshots):
            self._attr_snapshots.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self) -> None:
        for key, prev in self._env_snapshots.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        for obj, name, prev in self._attr_snapshots:
            setattr(obj, name, prev)


@pytest.fixture
def sdk_env():
    """Yield an ``_Env`` helper; all mutations auto-revert on teardown."""
    env = _Env()
    try:
        yield env
    finally:
        env.restore()


def _swap_registry(env: _Env, entry: Any) -> None:
    """Wire a fake Registry that returns ``entry`` from ``.get(name)``."""
    import scitex_agent_container._state.registry as reg_mod

    class _FakeRegistry:
        def get(self, _name):
            return entry

    env.setattr_module(reg_mod, "Registry", _FakeRegistry)


def _swap_load_config(env: _Env, workdir: str) -> None:
    """Wire a fake ``config.load_config`` that returns a stub w/ workdir."""
    import scitex_agent_container.config as cfg_mod

    env.setattr_module(
        cfg_mod, "load_config", lambda _path: SimpleNamespace(expanded_workdir=workdir)
    )


# ---------------------------------------------------------------------------
# provision_anthropic_auth
# ---------------------------------------------------------------------------


class TestProvisionAuth:
    def test_pre_set_anthropic_api_key_is_popped_when_sac_unset(
        self, sdk_env: _Env, tmp_path
    ):
        """A pre-set ``ANTHROPIC_API_KEY`` is *never* honoured.

        See the module-level comment in ``runtimes/_sdk_common.py``
        for the why — short version: stale dotfiles exports of
        ``ANTHROPIC_API_KEY`` shadow the credentials.json path and
        produce surprise pay-per-token billing or "401 Invalid auth"
        in production.
        """
        sdk_env.setenv("ANTHROPIC_API_KEY", "sk-ant-stale-from-dotfiles")
        sdk_env.delenv(_SAC_KEY)
        cred = tmp_path / ".credentials.json"
        cred.write_text('{"claudeAiOauth": {"accessToken": "tok"}}')
        sdk_env.setattr_module(_sdk_common, "_CRED_FILE", cred)

        assert provision_anthropic_auth() == "credentials_file"
        # The pre-set ANTHROPIC_API_KEY must have been popped so the
        # SDK auto-reader can't pick it up after we return.
        assert "ANTHROPIC_API_KEY" not in os.environ

    def test_sac_value_overrides_pre_set_anthropic_api_key(
        self, sdk_env: _Env, tmp_path
    ):
        """``SAC_ANTHROPIC_API_KEY`` is the only trusted env input — it
        overwrites a stale ``ANTHROPIC_API_KEY`` unconditionally."""
        sdk_env.setenv("ANTHROPIC_API_KEY", "sk-ant-stale-from-dotfiles")
        sdk_env.setenv(_SAC_KEY, "sk-ant-api-sac")
        sdk_env.setattr_module(_sdk_common, "_CRED_FILE", tmp_path / "missing")

        assert provision_anthropic_auth() == "sac_env"
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-api-sac"

    def test_credentials_file_wins_over_sac_env_no_env_shadow(
        self, sdk_env: _Env, tmp_path
    ):
        """Cred file wins, AND ANTHROPIC_API_KEY stays unset.

        Anthropic rejects ``sk-ant-oat*`` OAuth tokens passed as a
        bare ``ANTHROPIC_API_KEY`` env. The SDK's auto-reader prefers
        the env over the file when both are present — so even though
        the file is the "correct" auth path, mirroring the SAC value
        into ANTHROPIC_API_KEY would silently shadow the file and make
        the SDK use the rejected env path. The provisioner must NOT
        set ANTHROPIC_API_KEY when the file is the chosen path.
        """
        sdk_env.setenv("ANTHROPIC_API_KEY", "sk-ant-stale")
        cred = tmp_path / ".credentials.json"
        cred.write_text('{"claudeAiOauth": {"accessToken": "tok"}}')
        sdk_env.setattr_module(_sdk_common, "_CRED_FILE", cred)
        sdk_env.setenv(_SAC_KEY, "sk-ant-oat-sac")

        assert provision_anthropic_auth() == "credentials_file"
        # Critical: ANTHROPIC_API_KEY is NOT set so the SDK reads the
        # credentials file (which has a real refresh_token) instead of
        # the bare-bearer env that Anthropic would reject.
        assert "ANTHROPIC_API_KEY" not in os.environ

    def test_sac_env_when_no_credentials_file(self, sdk_env: _Env, tmp_path):
        """SAC value (any form) is mirrored to ANTHROPIC_API_KEY when
        the cred file is absent. Sac never writes credentials.json — the
        flow is one-way (cred file → SAC → ANTHROPIC), never the reverse."""
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.setattr_module(_sdk_common, "_CRED_FILE", tmp_path / "missing")
        sdk_env.setenv(_SAC_KEY, "sk-ant-api-sac")

        assert provision_anthropic_auth() == "sac_env"
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-api-sac"
        assert not (tmp_path / "missing").exists()

    def test_oauth_value_does_not_synthesise_credentials_file(
        self, sdk_env: _Env, tmp_path
    ):
        """``sk-ant-oat*`` SAC value flows through env override only —
        sac NEVER synthesises credentials.json."""
        sdk_env.delenv("ANTHROPIC_API_KEY")
        cred_target = tmp_path / ".claude" / ".credentials.json"
        sdk_env.setattr_module(_sdk_common, "_CRED_FILE", cred_target)
        sdk_env.setenv(_SAC_KEY, "sk-ant-oat-zzz")

        assert provision_anthropic_auth() == "sac_env"
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-oat-zzz"
        # Crucially, sac did NOT write the cred file.
        assert not cred_target.exists()

    def test_no_auth_raises(self, sdk_env: _Env, tmp_path):
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        sdk_env.setattr_module(_sdk_common, "_CRED_FILE", tmp_path / "missing")
        with pytest.raises(SDKCommonError):
            provision_anthropic_auth()


# ---------------------------------------------------------------------------
# resolve_agent_workspace
# ---------------------------------------------------------------------------


class TestResolveWorkspace:
    def test_unknown_agent_returns_empty(self, sdk_env: _Env):
        _swap_registry(sdk_env, None)
        assert resolve_agent_workspace("nope") == ({}, None)

    def test_workspace_without_mcp_json(self, sdk_env: _Env, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        _swap_registry(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config(sdk_env, str(ws))
        assert resolve_agent_workspace("alpha") == ({}, str(ws))

    def test_mcp_servers_parsed_with_env_substitution(self, sdk_env: _Env, tmp_path):
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
        sdk_env.setenv("MY_TOKEN", "tk-123")
        _swap_registry(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config(sdk_env, str(ws))
        servers, cwd = resolve_agent_workspace("alpha")
        assert cwd == str(ws)
        assert servers == {
            "stx": {
                "command": "scitex",
                "args": ["mcp", "--token", "tk-123"],
                "type": "stdio",
            }
        }

    def test_unresolved_env_ref_passes_through_literal(self, sdk_env: _Env, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"x": {"args": ["${UNSET_VAR}"]}}})
        )
        sdk_env.delenv("UNSET_VAR")
        _swap_registry(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config(sdk_env, str(ws))
        servers, _ = resolve_agent_workspace("alpha")
        assert servers["x"]["args"] == ["${UNSET_VAR}"]

    def test_malformed_mcp_json_tolerated(self, sdk_env: _Env, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".mcp.json").write_text("{not valid json")
        _swap_registry(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config(sdk_env, str(ws))
        servers, cwd = resolve_agent_workspace("alpha")
        assert servers == {}
        assert cwd == str(ws)


# ---------------------------------------------------------------------------
# build_sdk_options
# ---------------------------------------------------------------------------


class TestBuildOptions:
    def test_composes_all_layers(self, sdk_env: _Env, tmp_path):
        # Auth: pretend cred file exists so no env mutation.
        cred = tmp_path / ".credentials.json"
        cred.write_text("{}")
        sdk_env.setattr_module(_sdk_common, "_CRED_FILE", cred)
        sdk_env.delenv("ANTHROPIC_API_KEY")
        # Workspace: register a fake agent.
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"stx": {"command": "scitex"}}})
        )
        _swap_registry(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config(sdk_env, str(ws))

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

    def test_extra_kwargs_pass_through(self, sdk_env: _Env, tmp_path):
        cred = tmp_path / ".credentials.json"
        cred.write_text("{}")
        sdk_env.setattr_module(_sdk_common, "_CRED_FILE", cred)
        sdk_env.delenv("ANTHROPIC_API_KEY")
        _swap_registry(sdk_env, None)
        opts = build_sdk_options("nope", extra={"continue_conversation": True})
        assert opts.continue_conversation is True

    def test_missing_sdk_raises(self, sdk_env: _Env):
        """Simulate ``claude_agent_sdk`` not being installed."""
        import builtins

        real_import = builtins.__import__

        def _fake_import(name, *a, **kw):
            if name == "claude_agent_sdk":
                raise ImportError("simulated absence")
            return real_import(name, *a, **kw)

        sdk_env.setattr_module(builtins, "__import__", _fake_import)
        with pytest.raises(SDKCommonError, match="claude-agent-sdk is not installed"):
            build_sdk_options("any")
