"""Tests for ``runtimes/_sdk_common.py``.

Covers the three concerns the helper consolidates:

  * auth-path selection (env / credentials_file / bridged_sac / failure)
  * workspace + MCP-server resolution from the agent registry
  * ``ClaudeAgentOptions`` composition

PA-306: no `monkeypatch`. Env vars and module attributes are saved /
restored via a ``sdk_env`` fixture that yields a small ``Env`` helper.
``Env.setattr_module(_sdk_common, name, value)`` is the equivalent of
``monkeypatch.setattr`` with explicit teardown.

The credentials-file path is resolved at CALL time by
``_sdk_common._cred_file_path()`` (honouring ``CLAUDE_CONFIG_DIR`` /
``HOME``), NOT frozen at import. Tests therefore redirect it by pointing
``CLAUDE_CONFIG_DIR`` at a tmp dir via :func:`_redirect_cred_dir` — this
keeps the suite hermetic (no leak onto the operator's real
``~/.claude/.credentials.json``, which is why the old import-frozen
constant produced false greens locally yet failed in CI).

TQ cleanup: every test follows Arrange / Act / Assert with a single
assertion. Multi-fact scenarios are split into sibling tests sharing
a fixture so the failing-line tells you which contract regressed.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scitex_agent_container.runtimes import _sdk_common
from scitex_agent_container.runtimes._mcp_config_file import read_mcp_servers
from scitex_agent_container.runtimes._sdk_common import (
    SDKCommonError,
    build_sdk_options,
    provision_anthropic_auth,
    resolve_agent_workspace,
)

_SAC_KEY = _sdk_common._SAC_API_KEY_ENV


def _valid_creds_json() -> str:
    """Return a credentials.json body with a token valid for ~1 day.

    ``provision_anthropic_auth`` now runs the OAuth expiry check on the
    file before returning ``"credentials_file"`` (so an expired token
    fails LOUDLY instead of dying mid-session). Every fixture that writes
    a ``.credentials.json`` precondition needs a realistically *valid*
    token; ``expiresAt`` is unix milliseconds, far enough in the future
    to clear the 5-min skew.
    """
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


def _redirect_cred_dir(env: _Env, cfg_dir) -> "Path":
    """Point the call-time cred resolver at ``cfg_dir`` and return the path.

    ``_sdk_common._cred_file_path()`` honours ``CLAUDE_CONFIG_DIR`` first
    (``<dir>/.credentials.json``). Setting it here redirects
    ``provision_anthropic_auth`` off the operator's real
    ``~/.claude/.credentials.json`` and onto the test's tmp dir — so the
    suite is hermetic whether or not a real cred file exists on the host.
    Returns the resolved ``.credentials.json`` path under ``cfg_dir``.
    """
    env.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))
    return Path(cfg_dir) / ".credentials.json"


def _write_valid_cred(env: _Env, cfg_dir) -> "Path":
    """Redirect the cred dir to ``cfg_dir`` and write a VALID token there.

    Convenience for the many fixtures that only need *some* usable cred
    file present so ``provision_anthropic_auth`` returns
    ``"credentials_file"`` and lets the test exercise unrelated behaviour
    (settings flag, channel sidecar, kwargs pass-through, compose).
    """
    cred = _redirect_cred_dir(env, cfg_dir)
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text(_valid_creds_json())
    return cred


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
    # --- scenario: pre-set ANTHROPIC_API_KEY with SAC unset and cred file
    # present. See the module-level comment in ``runtimes/_sdk_common.py``
    # for the why — stale dotfiles exports of ANTHROPIC_API_KEY shadow the
    # credentials.json path and produce surprise pay-per-token billing or
    # "401 Invalid auth" in production.

    @pytest.fixture
    def _stale_env_with_cred(self, sdk_env: _Env, tmp_path):
        # Arrange
        sdk_env.setenv("ANTHROPIC_API_KEY", "sk-ant-stale-from-dotfiles")
        sdk_env.delenv(_SAC_KEY)
        _write_valid_cred(sdk_env, tmp_path)
        return sdk_env

    def test_pre_set_anthropic_api_key_returns_credentials_file(
        self, _stale_env_with_cred
    ):
        # Arrange (handled by fixture)
        # Act
        result = provision_anthropic_auth()
        # Assert
        assert result == "credentials_file"

    def test_pre_set_anthropic_api_key_is_popped(self, _stale_env_with_cred):
        # Arrange (handled by fixture)
        # Act
        provision_anthropic_auth()
        # Assert: the pre-set ANTHROPIC_API_KEY must have been popped so the
        # SDK auto-reader can't pick it up after we return.
        assert "ANTHROPIC_API_KEY" not in os.environ

    # --- scenario: SAC value overrides pre-set ANTHROPIC_API_KEY.
    # ``SAC_ANTHROPIC_API_KEY`` is the only trusted env input — it
    # overwrites a stale ``ANTHROPIC_API_KEY`` unconditionally.

    @pytest.fixture
    def _sac_overrides_stale(self, sdk_env: _Env, tmp_path):
        # Arrange
        sdk_env.setenv("ANTHROPIC_API_KEY", "sk-ant-stale-from-dotfiles")
        sdk_env.setenv(_SAC_KEY, "sk-ant-api-sac")
        # Empty cfg dir → no .credentials.json present.
        _redirect_cred_dir(sdk_env, tmp_path)
        return sdk_env

    def test_sac_value_returns_sac_env(self, _sac_overrides_stale):
        # Arrange (handled by fixture)
        # Act
        result = provision_anthropic_auth()
        # Assert
        assert result == "sac_env"

    def test_sac_value_mirrored_into_anthropic_api_key(self, _sac_overrides_stale):
        # Arrange (handled by fixture)
        # Act
        provision_anthropic_auth()
        # Assert
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-api-sac"

    # --- scenario: cred file wins over SAC env without shadowing.
    #
    # Anthropic rejects ``sk-ant-oat*`` OAuth tokens passed as a bare
    # ``ANTHROPIC_API_KEY`` env. The SDK's auto-reader prefers the env
    # over the file when both are present — so even though the file is
    # the "correct" auth path, mirroring the SAC value into
    # ANTHROPIC_API_KEY would silently shadow the file and make the SDK
    # use the rejected env path. The provisioner must NOT set
    # ANTHROPIC_API_KEY when the file is the chosen path.

    @pytest.fixture
    def _cred_and_sac_both_present(self, sdk_env: _Env, tmp_path):
        # Arrange
        sdk_env.setenv("ANTHROPIC_API_KEY", "sk-ant-stale")
        _write_valid_cred(sdk_env, tmp_path)
        sdk_env.setenv(_SAC_KEY, "sk-ant-oat-sac")
        return sdk_env

    def test_credentials_file_wins_over_sac_env(self, _cred_and_sac_both_present):
        # Arrange (handled by fixture)
        # Act
        result = provision_anthropic_auth()
        # Assert
        assert result == "credentials_file"

    def test_credentials_file_path_does_not_shadow_anthropic_api_key(
        self, _cred_and_sac_both_present
    ):
        # Arrange (handled by fixture)
        # Act
        provision_anthropic_auth()
        # Assert: ANTHROPIC_API_KEY is NOT set so the SDK reads the
        # credentials file (which has a real refresh_token) instead of
        # the bare-bearer env that Anthropic would reject.
        assert "ANTHROPIC_API_KEY" not in os.environ

    # --- scenario: SAC env when no credentials file. SAC value (any form)
    # is mirrored to ANTHROPIC_API_KEY when the cred file is absent. Sac
    # never writes credentials.json — the flow is one-way
    # (cred file -> SAC -> ANTHROPIC), never the reverse.

    @pytest.fixture
    def _only_sac(self, sdk_env: _Env, tmp_path):
        # Arrange — empty cfg dir → no .credentials.json present.
        sdk_env.delenv("ANTHROPIC_API_KEY")
        cred_target = _redirect_cred_dir(sdk_env, tmp_path)
        sdk_env.setenv(_SAC_KEY, "sk-ant-api-sac")
        return (sdk_env, cred_target)

    def test_sac_env_only_returns_sac_env(self, _only_sac):
        # Arrange (handled by fixture)
        # Act
        result = provision_anthropic_auth()
        # Assert
        assert result == "sac_env"

    def test_sac_env_only_mirrors_value_to_anthropic_api_key(self, _only_sac):
        # Arrange (handled by fixture)
        # Act
        provision_anthropic_auth()
        # Assert
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-api-sac"

    def test_sac_env_only_does_not_create_credentials_file(self, _only_sac):
        # Arrange
        _, cred_target = _only_sac
        # Act
        provision_anthropic_auth()
        # Assert
        assert not cred_target.exists()

    # --- scenario: ``sk-ant-oat*`` SAC value flows through env override
    # only — sac NEVER synthesises credentials.json.

    @pytest.fixture
    def _oat_value_only(self, sdk_env: _Env, tmp_path):
        # Arrange — cfg dir has no .credentials.json (and is never created).
        sdk_env.delenv("ANTHROPIC_API_KEY")
        cred_target = _redirect_cred_dir(sdk_env, tmp_path / ".claude")
        sdk_env.setenv(_SAC_KEY, "sk-ant-oat-zzz")
        return (sdk_env, cred_target)

    def test_oauth_value_returns_sac_env(self, _oat_value_only):
        # Arrange (handled by fixture)
        # Act
        result = provision_anthropic_auth()
        # Assert
        assert result == "sac_env"

    def test_oauth_value_mirrored_to_anthropic_api_key(self, _oat_value_only):
        # Arrange (handled by fixture)
        # Act
        provision_anthropic_auth()
        # Assert
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-oat-zzz"

    def test_oauth_value_does_not_synthesise_credentials_file(self, _oat_value_only):
        # Arrange
        _, cred_target = _oat_value_only
        # Act
        provision_anthropic_auth()
        # Assert: sac did NOT write the cred file.
        assert not cred_target.exists()

    def test_no_auth_raises(self, sdk_env: _Env, tmp_path):
        # Arrange — empty cfg dir → no .credentials.json present.
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        _redirect_cred_dir(sdk_env, tmp_path)
        # Act
        ctx = pytest.raises(SDKCommonError)
        # Assert
        with ctx:
            provision_anthropic_auth()

    # --- scenario: the credentials file exists but its OAuth token is
    # already expired. The file merely *existing* must NOT be treated as
    # usable auth — provision_anthropic_auth runs the expiry check and
    # fails LOUDLY with the manual-refresh hint, so the agent never opens
    # a session that dies with an ambiguous 401.

    @pytest.fixture
    def _expired_cred(self, sdk_env: _Env, tmp_path):
        # Arrange
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        cred = _redirect_cred_dir(sdk_env, tmp_path)
        expired_ms = int((time.time() - 86_400) * 1_000)
        cred.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": expired_ms}})
        )
        return sdk_env

    def test_expired_credentials_file_raises_loudly(self, _expired_cred):
        # Arrange (handled by fixture)
        # Act
        ctx = pytest.raises(SDKCommonError)
        # Assert
        with ctx:
            provision_anthropic_auth()

    def test_expired_credentials_error_carries_refresh_hint(self, _expired_cred):
        # Arrange (handled by fixture)
        # Act
        try:
            provision_anthropic_auth()
            message = ""
        except SDKCommonError as exc:
            message = str(exc)
        # Assert
        assert "claude login" in message


# ---------------------------------------------------------------------------
# resolve_agent_workspace
# ---------------------------------------------------------------------------


class TestResolveWorkspace:
    def test_unknown_agent_returns_empty(self, sdk_env: _Env):
        # Arrange
        _swap_registry(sdk_env, None)
        # Act
        result = resolve_agent_workspace("nope")
        # Assert
        assert result == ({}, None)

    def test_workspace_without_mcp_json(self, sdk_env: _Env, tmp_path):
        # Arrange
        ws = tmp_path / "ws"
        ws.mkdir()
        _swap_registry(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config(sdk_env, str(ws))
        # Act
        result = resolve_agent_workspace("alpha")
        # Assert
        assert result == ({}, str(ws))

    # --- scenario: MCP servers parsed with env substitution. Split into
    # cwd-check and servers-shape-check (sharing the same fixture).

    @pytest.fixture
    def _mcp_env_substitution(self, sdk_env: _Env, tmp_path):
        # Arrange
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
        return ws

    def test_mcp_servers_resolution_returns_workspace_cwd(self, _mcp_env_substitution):
        # Arrange (handled by fixture)
        ws = _mcp_env_substitution
        # Act
        _, cwd = resolve_agent_workspace("alpha")
        # Assert
        assert cwd == str(ws)

    def test_mcp_servers_resolution_substitutes_env_var(self, _mcp_env_substitution):
        # Arrange (handled by fixture)
        # Act
        servers, _ = resolve_agent_workspace("alpha")
        # Assert
        assert servers == {
            "stx": {
                "command": "scitex",
                "args": ["mcp", "--token", "tk-123"],
                "type": "stdio",
            }
        }

    def test_unresolved_env_ref_passes_through_literal(self, sdk_env: _Env, tmp_path):
        # Arrange
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"x": {"args": ["${UNSET_VAR}"]}}})
        )
        sdk_env.delenv("UNSET_VAR")
        _swap_registry(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config(sdk_env, str(ws))
        # Act
        servers, _ = resolve_agent_workspace("alpha")
        # Assert
        assert servers["x"]["args"] == ["${UNSET_VAR}"]

    # --- scenario: malformed mcp.json is tolerated.

    @pytest.fixture
    def _malformed_mcp(self, sdk_env: _Env, tmp_path):
        # Arrange
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".mcp.json").write_text("{not valid json")
        _swap_registry(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config(sdk_env, str(ws))
        return ws

    def test_malformed_mcp_json_yields_empty_servers(self, _malformed_mcp):
        # Arrange (handled by fixture)
        # Act
        servers, _ = resolve_agent_workspace("alpha")
        # Assert
        assert servers == {}

    def test_malformed_mcp_json_still_returns_workspace_cwd(self, _malformed_mcp):
        # Arrange (handled by fixture)
        ws = _malformed_mcp
        # Act
        _, cwd = resolve_agent_workspace("alpha")
        # Assert
        assert cwd == str(ws)


# ---------------------------------------------------------------------------
# build_sdk_options
# ---------------------------------------------------------------------------


class TestBuildOptions:
    # --- scenario: composes all layers (auth + workspace + kwargs). Split
    # per-layer assertion so a failure pinpoints the broken layer.

    @pytest.fixture
    def _composed_opts(self, sdk_env: _Env, tmp_path):
        # Arrange: a VALID cred file present so auth resolves to the
        # credentials_file path (no env-key mutation needed).
        _write_valid_cred(sdk_env, tmp_path)
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        # Arrange: register a fake agent with one MCP server.
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"stx": {"command": "scitex"}}})
        )
        _swap_registry(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config(sdk_env, str(ws))
        # Act
        opts = build_sdk_options(
            "alpha",
            system_prompt="be brief",
            model="claude-haiku-4-5",
            permission_mode="bypassPermissions",
        )
        return (opts, ws)

    @pytest.mark.parametrize(
        ("attr", "expected"),
        [
            ("system_prompt", "be brief"),
            ("model", "claude-haiku-4-5"),
            ("permission_mode", "bypassPermissions"),
        ],
    )
    def test_compose_propagates_kwarg(self, _composed_opts, attr, expected):
        # Arrange
        opts, _ = _composed_opts
        # Act
        actual = getattr(opts, attr)
        # Assert
        assert actual == expected

    def test_compose_sets_cwd_from_workspace(self, _composed_opts):
        # Arrange
        opts, ws = _composed_opts
        # Act
        cwd = str(opts.cwd)
        # Assert
        assert cwd == str(ws)

    def test_compose_enables_agent_tool_for_subagents(self, _composed_opts):
        # Arrange
        opts, _ = _composed_opts
        # Act
        allowed = list(opts.allowed_tools or [])
        # Assert
        assert "Agent" in allowed

    @pytest.fixture
    def _opts_preset_tools(self, sdk_env: _Env, tmp_path):
        # Arrange: same auth + workspace setup as _composed_opts, but the
        # caller pre-sets allowed_tools via ``extra`` to prove the merge
        # preserves the caller's list AND appends "Agent".
        _write_valid_cred(sdk_env, tmp_path)
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"stx": {"command": "scitex"}}})
        )
        _swap_registry(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config(sdk_env, str(ws))
        # Act
        opts = build_sdk_options(
            "alpha",
            permission_mode="bypassPermissions",
            extra={"allowed_tools": ["Read", "Bash"]},
        )
        return list(opts.allowed_tools or [])

    def test_compose_preserves_caller_allowed_tools(self, _opts_preset_tools):
        # Arrange
        allowed = _opts_preset_tools
        # Act
        preserved = "Read" in allowed and "Bash" in allowed
        # Assert
        assert preserved is True

    def test_compose_appends_agent_to_caller_allowed_tools(self, _opts_preset_tools):
        # Arrange
        allowed = _opts_preset_tools
        # Act
        has_agent = "Agent" in allowed
        # Assert
        assert has_agent is True

    def test_compose_attaches_mcp_servers(self, _composed_opts):
        # Arrange
        opts, _ = _composed_opts
        # Act
        servers = read_mcp_servers(opts.mcp_servers)
        # Assert
        assert "stx" in servers

    def test_extra_kwargs_pass_through(self, sdk_env: _Env, tmp_path):
        # Arrange
        _write_valid_cred(sdk_env, tmp_path)
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        _swap_registry(sdk_env, None)
        # Act
        opts = build_sdk_options("nope", extra={"continue_conversation": True})
        # Assert
        assert opts.continue_conversation is True

    def test_missing_sdk_raises(self, sdk_env: _Env):
        """Simulate ``claude_agent_sdk`` not being installed."""
        # Arrange
        import builtins

        real_import = builtins.__import__

        def _fake_import(name, *a, **kw):
            if name == "claude_agent_sdk":
                raise ImportError("simulated absence")
            return real_import(name, *a, **kw)

        sdk_env.setattr_module(builtins, "__import__", _fake_import)
        # Act
        ctx = pytest.raises(SDKCommonError, match="claude-agent-sdk is not installed")
        # Assert
        with ctx:
            build_sdk_options("any")


# ---------------------------------------------------------------------------
# PR #319 (lead msg a456b610 2026-06-06): provider-aware tool REGISTRATION
# whitelist. When ``spec.claude.provider`` declares a non-Anthropic backend
# (LiteLLM / vLLM / gateway), populate ``ClaudeAgentOptions.tools`` so the
# CLI only REGISTERS the shim-recognized tool set — newer Claude Code
# builtins (ExitPlanMode, BashOutput, KillShell) never enter the outbound
# API request body. Root cause: LiteLLM 1.52.16's pydantic Union mis-routes
# unrecognized tools → AnthropicComputerTool → 422 every turn.
#
# Resolution order tested:
#   1. spec.claude.provider.allowed_tools (operator override) — verbatim.
#   2. _PROVIDER_DEFAULT_ALLOWED_TOOLS (runner default) — when no spec list.
#   3. Caller-supplied ``extra={"tools": ...}`` WINS over both above.
#   4. No provider in cfg → ``tools`` left unset (back-compat).
# ---------------------------------------------------------------------------


def _swap_load_config_with_provider_tools(
    env: _Env, workdir: str, *, base_url: str, allowed_tools: list[str] | None = None
) -> None:
    """Wire load_config to return a stub config with provider + allowed_tools.

    The runner's provider gate consults
    ``config.claude.provider.base_url`` (non-empty → active). We also
    expose ``provider.allowed_tools`` so PR #319 can resolve from it.
    Uses real SimpleNamespace (no monkeypatch of the predicate itself).
    """
    import scitex_agent_container.config as cfg_mod

    provider_ns = SimpleNamespace(
        base_url=base_url,
        auth_token_env="API_KEY_ENV",
        allowed_tools=list(allowed_tools or []),
    )
    claude_ns = SimpleNamespace(provider=provider_ns, account="")
    config_ns = SimpleNamespace(expanded_workdir=workdir, claude=claude_ns)
    env.setattr_module(cfg_mod, "load_config", lambda _path: config_ns)


class TestProviderToolsWhitelist:
    @pytest.fixture
    def _opts_provider_default_tools(self, sdk_env: _Env, tmp_path):
        # Arrange — provider active, allowed_tools NOT set in spec.
        _write_valid_cred(sdk_env, tmp_path)
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        ws = tmp_path / "ws"
        ws.mkdir()
        _swap_registry(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config_with_provider_tools(
            sdk_env, str(ws), base_url="http://127.0.0.1:4000", allowed_tools=[]
        )
        # Act
        opts = build_sdk_options("alpha", permission_mode="bypassPermissions")
        return opts

    def test_default_tools_excludes_exit_plan_mode(self, _opts_provider_default_tools):
        # Arrange
        opts = _opts_provider_default_tools
        # Act
        tools = list(opts.tools or [])
        # Assert — the LiteLLM-1.52.16-incompatible builtin must NOT be
        # in the runner-default whitelist; clew's v8 422 cascade was
        # rooted in ExitPlanMode at tools[9] of the outbound body.
        assert "ExitPlanMode" not in tools

    def test_default_tools_includes_bash(self, _opts_provider_default_tools):
        # Arrange
        opts = _opts_provider_default_tools
        # Act
        tools = list(opts.tools or [])
        # Assert — sanity: Bash is the most basic Claude Code builtin.
        assert "Bash" in tools

    def test_default_tools_includes_agent_for_subagents(
        self, _opts_provider_default_tools
    ):
        # Arrange — Agent must be in the registration list AND
        # allowed_tools (the existing subagent enablement); the PR #319
        # default carries it so both lists stay coherent.
        opts = _opts_provider_default_tools
        # Act
        tools = list(opts.tools or [])
        # Assert
        assert "Agent" in tools

    @pytest.fixture
    def _opts_provider_spec_tools(self, sdk_env: _Env, tmp_path):
        # Arrange — operator OVERRIDES the default with an explicit
        # allowed_tools list. The runner must honour it verbatim.
        _write_valid_cred(sdk_env, tmp_path)
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        ws = tmp_path / "ws"
        ws.mkdir()
        _swap_registry(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config_with_provider_tools(
            sdk_env,
            str(ws),
            base_url="http://127.0.0.1:4000",
            allowed_tools=["Bash", "Read"],
        )
        # Act
        opts = build_sdk_options("alpha", permission_mode="bypassPermissions")
        return opts

    def test_spec_allowed_tools_overrides_default(self, _opts_provider_spec_tools):
        # Arrange
        opts = _opts_provider_spec_tools
        # Act
        tools = list(opts.tools or [])
        # Assert — the operator's explicit list wins; the default
        # (which would have added Edit/Write/Glob/etc.) must NOT leak.
        assert tools == ["Bash", "Read"]

    @pytest.fixture
    def _opts_no_provider(self, sdk_env: _Env, tmp_path):
        # Arrange — back-compat: real Anthropic backend (no provider
        # block). PR #319's auto-populate must NOT touch ``tools``.
        _write_valid_cred(sdk_env, tmp_path)
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        ws = tmp_path / "ws"
        ws.mkdir()
        _swap_registry(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config(sdk_env, str(ws))  # no provider on the config
        # Act
        opts = build_sdk_options("alpha", permission_mode="bypassPermissions")
        return opts

    def test_no_provider_leaves_tools_unset(self, _opts_no_provider):
        # Arrange
        opts = _opts_no_provider
        # Act
        tools = opts.tools
        # Assert — the CLI's full default toolset registers; suppressing
        # ExitPlanMode/BashOutput/KillShell on an Anthropic backend that
        # supports them would be a regression.
        assert tools is None

    @pytest.fixture
    def _opts_caller_tools_wins(self, sdk_env: _Env, tmp_path):
        # Arrange — provider active AND caller pre-supplies tools=
        # via ``extra``. The caller's explicit value MUST win — the
        # PR #319 auto-populate is "only when caller didn't say".
        _write_valid_cred(sdk_env, tmp_path)
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        ws = tmp_path / "ws"
        ws.mkdir()
        _swap_registry(sdk_env, {"config": "cfg.yaml"})
        _swap_load_config_with_provider_tools(
            sdk_env, str(ws), base_url="http://127.0.0.1:4000", allowed_tools=[]
        )
        # Act
        opts = build_sdk_options(
            "alpha",
            permission_mode="bypassPermissions",
            extra={"tools": ["OnlyThisOne"]},
        )
        return opts

    def test_caller_tools_extra_wins_over_auto_populate(self, _opts_caller_tools_wins):
        # Arrange
        opts = _opts_caller_tools_wins
        # Act
        tools = list(opts.tools or [])
        # Assert
        assert tools == ["OnlyThisOne"]


# ---------------------------------------------------------------------------
# build_sdk_options — explicit --settings load (hooks/settings)
#
# setting_sources stays [] (machine-independence: no host ~/.claude
# auto-discovery). The agent's settings.local.json is delivered into the
# container $HOME/.claude/ via the to_home mirror; build_sdk_options must
# point the SDK ``settings`` field (=> --settings, the highest-priority
# flag-settings layer) at it so hooks load independently of setting_sources.
# ---------------------------------------------------------------------------


class TestSettingsFlag:
    @pytest.fixture
    def _opts_with_settings(self, sdk_env: _Env, tmp_path):
        # Arrange — auth via a VALID cred file (resolved from
        # CLAUDE_CONFIG_DIR, independent of the $HOME we set below for
        # settings resolution), and a container $HOME holding the
        # delivered settings.local.json.
        _write_valid_cred(sdk_env, tmp_path / "cfg")
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        home = tmp_path / "home" / "agent"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.local.json").write_text('{"hooks": {}}\n')
        sdk_env.setenv("HOME", str(home))
        _swap_registry(sdk_env, None)
        # Act
        opts = build_sdk_options("alpha")
        return opts, home

    def test_settings_points_at_container_home_settings(self, _opts_with_settings):
        # Arrange
        opts, home = _opts_with_settings
        # Act
        # Assert — the in-container path, derived from $HOME.
        assert opts.settings == str(home / ".claude" / "settings.local.json")

    def test_setting_sources_stays_empty(self, _opts_with_settings):
        # Arrange — machine-independence invariant must NOT change.
        opts, _ = _opts_with_settings
        # Act
        # Assert
        assert opts.setting_sources == []

    def test_no_settings_when_file_absent(self, sdk_env: _Env, tmp_path):
        # Arrange — $HOME without a settings.local.json → no --settings.
        # Cred resolves from CLAUDE_CONFIG_DIR, independent of $HOME.
        _write_valid_cred(sdk_env, tmp_path / "cfg")
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        empty_home = tmp_path / "empty_home"
        empty_home.mkdir()
        sdk_env.setenv("HOME", str(empty_home))
        _swap_registry(sdk_env, None)
        # Act
        opts = build_sdk_options("alpha")
        # Assert
        assert opts.settings is None


# ---------------------------------------------------------------------------
# build_sdk_options — server:sac channel sidecar registration
# ---------------------------------------------------------------------------


class TestChannelSidecar:
    """``channels=[server:sac]`` registers the ``sac mcp channel`` adapter.

    Regression guard: the adapter subscribes to ``/agents/<name>/inbox/
    stream`` which is served by the BUS (``sac listen``, resolved from
    ``SAC_LISTEN_BASE_URL``), NOT the agent's own a2a sidecar port. An
    earlier version hardcoded ``--listen-url http://127.0.0.1:{a2a_port}``,
    pointing the SSE GET at a server that 404s on the inbox route, so the
    bus saw zero subscribers and ``delivered_subscriber_count`` was always
    0. These tests pin: the ``sac`` stdio MCP is registered, and its args
    never carry an a2a_port-derived ``--listen-url``.
    """

    @pytest.fixture
    def _fake_sac_bin(self, sdk_env: _Env, tmp_path):
        """Materialize an executable fake ``sac`` and point ``$SAC_BIN`` at it
        so ``apply_channels``' binary resolver returns a deterministic
        absolute path instead of raising ``SacBinaryNotFoundError``. Yields
        the absolute path so assertions can pin the exact value."""
        fake = tmp_path / "sac"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        sdk_env.setenv("SAC_BIN", str(fake))
        return str(fake)

    @pytest.fixture
    def _sac_channel_opts(self, sdk_env: _Env, tmp_path, _fake_sac_bin):
        # Arrange: a VALID cred file present so auth needs no env mutation.
        _write_valid_cred(sdk_env, tmp_path)
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        _swap_registry(sdk_env, None)
        # Act: thread the same sac-private extra the runner sends, including
        # an a2a_port that must NOT leak into the channel sidecar args.
        opts = build_sdk_options(
            "lead",
            extra={"_channels": ["server:sac"], "_a2a_port": 9999},
        )
        return opts

    def test_registers_sac_stdio_mcp(self, _sac_channel_opts, _fake_sac_bin):
        # Arrange
        # ``mcp_servers`` is a 0600 FILE PATH now — the assembled config must
        # not ride the world-readable child argv (runtimes/_mcp_config_file).
        servers = read_mcp_servers(_sac_channel_opts.mcp_servers)
        # Act
        sac = servers.get("sac")
        # Assert — resolver wires the SAC_BIN-overridden absolute path
        assert sac is not None and sac["command"] == _fake_sac_bin

    def test_sidecar_args_subscribe_to_named_agent_inbox(self, _sac_channel_opts):
        # Arrange
        sac = read_mcp_servers(_sac_channel_opts.mcp_servers)["sac"]
        # Act
        args = sac["args"]
        # Assert
        assert args[:4] == ["mcp", "channel", "--name", "lead"]

    def test_sidecar_args_omit_a2a_port_listen_url(self, _sac_channel_opts):
        # Arrange
        sac = read_mcp_servers(_sac_channel_opts.mcp_servers)["sac"]
        args = sac["args"]
        # Act — find the --listen-url value, if any.
        if "--listen-url" in args:
            listen_url = args[args.index("--listen-url") + 1]
        else:
            listen_url = None
        # Assert: the a2a_port (9999) must NOT be the BUS listen-url — bus URL
        # resolution is delegated to the adapter's main() via
        # SAC_LISTEN_BASE_URL. (It DOES appear as --turn-url, the agent's own
        # /v1/turn wake target — a distinct concern, covered separately.)
        assert listen_url is None or "9999" not in str(listen_url)

    def test_sidecar_args_carry_turn_url_for_wake(self, _sac_channel_opts):
        """WI-1: the a2a_port is threaded as ``--turn-url`` so the adapter can
        POST received bus events to the agent's own /v1/turn and WAKE an idle
        session (push ≡ Telegram)."""
        # Arrange
        sac = read_mcp_servers(_sac_channel_opts.mcp_servers)["sac"]
        args = sac["args"]
        # Act
        turn_url = args[args.index("--turn-url") + 1] if "--turn-url" in args else None
        # Assert — points at the agent's own loopback /v1/turn on the a2a_port.
        assert turn_url == "http://127.0.0.1:9999/v1/turn"

    def test_sidecar_listen_url_when_present_is_not_a2a_port(self, _sac_channel_opts):
        # Arrange
        sac = read_mcp_servers(_sac_channel_opts.mcp_servers)["sac"]
        args = sac["args"]
        # Act: if a --listen-url is emitted at all, it must be the bus, never
        # the agent's own a2a sidecar port.
        if "--listen-url" in args:
            listen_url = args[args.index("--listen-url") + 1]
        else:
            listen_url = None
        # Assert
        assert listen_url is None or listen_url == os.environ.get("SAC_LISTEN_BASE_URL")

    def test_no_error_when_a2a_port_absent(
        self, sdk_env: _Env, tmp_path, _fake_sac_bin
    ):
        # Arrange: valid cred present; channels set but NO _a2a_port threaded.
        # ``_fake_sac_bin`` provides $SAC_BIN so the binary resolver does not
        # raise SacBinaryNotFoundError on test hosts that lack ``sac`` on PATH.
        _write_valid_cred(sdk_env, tmp_path)
        sdk_env.delenv("ANTHROPIC_API_KEY")
        sdk_env.delenv(_SAC_KEY)
        _swap_registry(sdk_env, None)
        # Act: a2a_port is irrelevant to inbox subscription, so its absence
        # must NOT raise — the adapter resolves the bus from env at runtime.
        opts = build_sdk_options("lead", extra={"_channels": ["server:sac"]})
        # Assert
        assert "sac" in read_mcp_servers(opts.mcp_servers)
