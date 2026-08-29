"""Tests for runtimes._mcp_spec_env — durable spec env for MCP respawns.

P1, card sac-env-injection-lost-on-mcp-reconnect-20260721: spec env used to
reach MCP servers ONLY by process inheritance, which a mid-session reconnect
RESPAWN through the sanitized stdio transport env does not provide. These
tests simulate that respawn exactly the way the deployed Claude Code stdio
transport builds the child env — ``{**allowlist_base, **entry_env}`` with the
POSIX allowlist ``HOME/LOGNAME/PATH/SHELL/TERM/USER`` (read out of the
claude 2.1.216 binary) — and assert the spec env still arrives. Removing the
bake (the fix) turns the respawn tests red while first-spawn inheritance
would still look green, which is exactly the trap the incident sprang.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scitex_agent_container.runtimes._board_identity_env import (
    UnexpandedEnvValueError,
)
from scitex_agent_container.runtimes._mcp_config_file import read_mcp_servers
from scitex_agent_container.runtimes._mcp_spec_env import (
    SPEC_ENV_KEYS_VAR,
    SpecEnvUnresolvedError,
    bake_spec_env_into_servers,
    bake_spec_env_values,
    resolve_spec_env,
    spec_env_keys_flag,
)

# The sanitized base env the deployed stdio transport gives a respawned MCP
# child (POSIX allowlist, extracted from the claude 2.1.216 bundle:
# ``["HOME","LOGNAME","PATH","SHELL","TERM","USER"]``). The respawn
# simulation below spreads the entry env over THIS base — nothing else.
_RESPAWN_ALLOWLIST = ("HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER")


def _respawned_child_env(entry: dict) -> dict[str, str]:
    """Build the env a RECONNECT respawn hands the stdio child.

    Mirrors the deployed transport's ``{...getDefaultEnvironment(),
    ...serverParams.env}`` shape: allowlist base from the live process env,
    then the entry's declared env block — and NOTHING else (no parent-env
    inheritance; that is the whole incident).
    """
    base = {k: os.environ[k] for k in _RESPAWN_ALLOWLIST if k in os.environ}
    return {**base, **(entry.get("env") or {})}


class _Env:
    """Explicit env mutator with restore on teardown (PA-306, no mocks)."""

    def __init__(self) -> None:
        self._snapshots: dict[str, str | None] = {}
        self._attrs: list[tuple[Any, str, Any]] = []

    def setenv(self, key: str, value: str) -> None:
        self._snapshots.setdefault(key, os.environ.get(key))
        os.environ[key] = value

    def delenv(self, key: str) -> None:
        self._snapshots.setdefault(key, os.environ.get(key))
        os.environ.pop(key, None)

    def setattr_module(self, obj: Any, name: str, value: Any) -> None:
        if not any(a is obj and n == name for a, n, _ in self._attrs):
            self._attrs.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self) -> None:
        for key, prev in self._snapshots.items():
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
# spec_env_keys_flag — the launch-side manifest
# ---------------------------------------------------------------------------


def test_spec_env_keys_flag_names_every_injected_key() -> None:
    # Arrange
    agent_env = {"SCITEX_CARDS_DB": "/x/cards.db", "A_VAR": "1"}
    # Act
    flags = spec_env_keys_flag(agent_env)
    # Assert
    assert flags == ["--env", f"{SPEC_ENV_KEYS_VAR}=A_VAR,SCITEX_CARDS_DB"]


def test_spec_env_keys_flag_empty_env_emits_no_flag() -> None:
    # Arrange
    agent_env: dict[str, str] = {}
    # Act
    flags = spec_env_keys_flag(agent_env)
    # Assert
    assert flags == []


# ---------------------------------------------------------------------------
# resolve_spec_env — the container-side manifest read
# ---------------------------------------------------------------------------


def test_resolve_spec_env_returns_the_injected_values() -> None:
    # Arrange
    environ = {
        SPEC_ENV_KEYS_VAR: "STORE_VAR,OTHER_VAR",
        "STORE_VAR": "/shared/tasks.yaml",
        "OTHER_VAR": "x",
        "AMBIENT": "noise",
    }
    # Act
    resolved = resolve_spec_env(environ)
    # Assert
    assert resolved == {"STORE_VAR": "/shared/tasks.yaml", "OTHER_VAR": "x"}


def test_resolve_spec_env_without_manifest_is_a_noop() -> None:
    # Arrange: a container launched by a pre-manifest sac (rolling deploy).
    environ = {"STORE_VAR": "/shared/tasks.yaml"}
    # Act
    resolved = resolve_spec_env(environ)
    # Assert
    assert resolved == {}


def test_resolve_spec_env_missing_key_fails_loud_naming_it() -> None:
    # Arrange: the manifest promises a key the environment does not carry.
    environ = {SPEC_ENV_KEYS_VAR: "STORE_VAR"}
    # Act
    raised = pytest.raises(SpecEnvUnresolvedError, match="STORE_VAR")
    # Assert
    with raised:
        resolve_spec_env(environ)


def test_resolve_spec_env_rejects_an_unexpanded_value() -> None:
    # Arrange: the value is the SHAPE of a substitution that never happened
    # (INCIDENT 2026-07-19: literal ${VAR} stored as a card author).
    environ = {
        SPEC_ENV_KEYS_VAR: "AGENT_ID",
        "AGENT_ID": "${SCITEX_CARDS_AGENT_ID}",
    }
    # Act
    raised = pytest.raises(UnexpandedEnvValueError)
    # Assert
    with raised:
        resolve_spec_env(environ)


# ---------------------------------------------------------------------------
# bake_spec_env_values — the config-side bake
# ---------------------------------------------------------------------------


def test_bake_adds_spec_env_to_a_stdio_entry_env_block() -> None:
    # Arrange
    servers = {"cards": {"type": "stdio", "command": "scitex-cards"}}
    # Act
    bake_spec_env_values(servers, {"STORE_VAR": "/shared/tasks.yaml"})
    # Assert
    assert servers["cards"]["env"]["STORE_VAR"] == "/shared/tasks.yaml"


def test_bake_treats_a_typeless_entry_as_stdio() -> None:
    # Arrange: entries without ``type`` default to stdio (Claude Code / SDK).
    servers = {"cards": {"command": "scitex-cards"}}
    # Act
    bake_spec_env_values(servers, {"STORE_VAR": "/shared/tasks.yaml"})
    # Assert
    assert servers["cards"]["env"]["STORE_VAR"] == "/shared/tasks.yaml"


def test_bake_never_overrides_an_entry_declared_env_key() -> None:
    # Arrange: the entry pins its own value for the key.
    servers = {
        "cards": {
            "type": "stdio",
            "command": "scitex-cards",
            "env": {"STORE_VAR": "/pinned/tasks.yaml"},
        }
    }
    # Act
    bake_spec_env_values(servers, {"STORE_VAR": "/shared/tasks.yaml"})
    # Assert
    assert servers["cards"]["env"]["STORE_VAR"] == "/pinned/tasks.yaml"


def test_bake_leaves_http_entries_untouched() -> None:
    # Arrange: no child process to spawn — an env block would be dead weight.
    servers = {"remote": {"type": "http", "url": "https://example.test/mcp"}}
    # Act
    bake_spec_env_values(servers, {"STORE_VAR": "/shared/tasks.yaml"})
    # Assert
    assert "env" not in servers["remote"]


# ---------------------------------------------------------------------------
# THE MUTATION PROOF — the respawn simulation
# ---------------------------------------------------------------------------


def test_respawned_stdio_child_still_receives_the_store_var() -> None:
    """A reconnect respawn must deliver the spec env the first spawn had.

    Simulates the exact deployed respawn env construction (sanitized
    allowlist base + entry env, NO parent inheritance). With the bake
    removed, the store var vanishes from the child env — the live incident
    (resolve-store flipping to a different store mid-session).
    """
    # Arrange: the entry as sac assembles it (no store var declared), and a
    # container env carrying the spec env + the launch manifest.
    servers = {"cards": {"type": "stdio", "command": "scitex-cards"}}
    environ = dict(os.environ)
    environ[SPEC_ENV_KEYS_VAR] = "SCITEX_TODO_TASKS_YAML_SHARED"
    environ["SCITEX_TODO_TASKS_YAML_SHARED"] = "/shared/store/tasks.yaml"
    # Act: bake (the fix), then respawn the child WITHOUT inheritance.
    bake_spec_env_into_servers(servers, environ)
    child_env = _respawned_child_env(servers["cards"])
    # Assert: the store-selecting var survives the sanitized respawn.
    assert child_env["SCITEX_TODO_TASKS_YAML_SHARED"] == "/shared/store/tasks.yaml"


def test_first_spawn_inheritance_alone_does_not_survive_the_respawn() -> None:
    """Control: the pre-fix delivery (process inheritance only) is LOST.

    Proves the simulation actually models the incident — a var present in
    the parent process env but not baked into the entry does NOT reach the
    respawned child, so a green first spawn says nothing about the respawn.
    """
    # Arrange: var in the parent env, NOT in the entry env block.
    servers = {"cards": {"type": "stdio", "command": "scitex-cards"}}
    environ = dict(os.environ)
    environ["SCITEX_TODO_TASKS_YAML_SHARED"] = "/shared/store/tasks.yaml"
    # Act: no manifest -> nothing baked; respawn without inheritance.
    bake_spec_env_into_servers(servers, environ)
    child_env = _respawned_child_env(servers["cards"])
    # Assert: the volatile channel does not survive.
    assert "SCITEX_TODO_TASKS_YAML_SHARED" not in child_env


# ---------------------------------------------------------------------------
# Wiring: the launch argv carries the manifest (listen_env_flags)
# ---------------------------------------------------------------------------


def test_listen_env_flags_carries_the_spec_env_manifest(env: _Env) -> None:
    # Arrange: a minimal spec-shaped config with one env var; no bus channel
    # so an absent bearer token only warns.
    from scitex_agent_container.runtimes._apptainer_listen_env import (
        listen_env_flags,
    )

    config = SimpleNamespace(
        name="agent-x",
        env={"SCITEX_CARDS_DB": "/shared/cards.db"},
        claude=None,
        apptainer=None,
    )
    # Act
    flags = listen_env_flags(config)
    manifests = [
        f.split("=", 1)[1] for f in flags if f.startswith(f"{SPEC_ENV_KEYS_VAR}=")
    ]
    # Assert: exactly one manifest flag, naming the spec key.
    assert len(manifests) == 1 and "SCITEX_CARDS_DB" in manifests[0].split(",")


# ---------------------------------------------------------------------------
# Wiring: build_sdk_options bakes into the options the respawner reads
# ---------------------------------------------------------------------------


def _valid_creds_json() -> str:
    """A credentials.json valid for ~1 day (mirrors test__sdk_common)."""
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


def test_build_sdk_options_bakes_manifested_spec_env_into_servers(
    env: _Env, tmp_path: Path
) -> None:
    """The SDK chokepoint delivers spec env into the config the respawner
    reads. Removing the ``bake_spec_env_into_servers`` call in
    ``build_sdk_options`` turns this red."""
    # Arrange: hermetic auth (valid cred under a redirected config dir).
    pytest.importorskip("claude_agent_sdk")
    from scitex_agent_container.runtimes import _sdk_common

    env.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    cred = tmp_path / ".credentials.json"
    cred.write_text(_valid_creds_json())
    env.delenv("ANTHROPIC_API_KEY")
    env.delenv(_sdk_common._SAC_API_KEY_ENV)
    # Arrange: a registered workspace with one MCP server and no env block.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"stx": {"command": "scitex"}}})
    )
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
    # Arrange: the launch manifest + the spec env value in this process env.
    env.setenv("SCITEX_CARDS_DB", "/shared/cards.db")
    env.setenv(SPEC_ENV_KEYS_VAR, "SCITEX_CARDS_DB")
    # Act
    opts = _sdk_common.build_sdk_options("alpha")
    # Assert: the entry the respawner reads carries the literal value.
    # ``mcp_servers`` is a 0600 FILE PATH now (secrets must not ride the child
    # argv — see runtimes/_mcp_config_file), so read the effective table back.
    servers = read_mcp_servers(opts.mcp_servers)
    assert servers["stx"]["env"]["SCITEX_CARDS_DB"] == "/shared/cards.db"


# ---------------------------------------------------------------------------
# Wiring: the workdir .mcp.json writer bakes spec.env durably
# ---------------------------------------------------------------------------


def test_setup_mcp_config_bakes_spec_env_into_written_entries(
    tmp_path: Path,
) -> None:
    # Arrange
    from scitex_agent_container.config import AgentConfig
    from scitex_agent_container.runtimes.mcp_config import setup_mcp_config

    cfg = AgentConfig(
        name="agent-x",
        mcp_servers={"cards": {"command": "scitex-cards"}},
        env={"SCITEX_CARDS_DB": "/shared/cards.db"},
    )
    # Act
    setup_mcp_config(cfg, str(tmp_path))
    # Assert
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert data["mcpServers"]["cards"]["env"]["SCITEX_CARDS_DB"] == "/shared/cards.db"


def test_setup_mcp_config_rejects_an_unresolvable_spec_env_ref(
    env: _Env, tmp_path: Path
) -> None:
    # Arrange: spec.env value references a var that is NOT set anywhere, so
    # write-time resolution leaves the literal ${...} — which must fail loud
    # instead of being baked as data.
    from scitex_agent_container.config import AgentConfig
    from scitex_agent_container.runtimes.mcp_config import setup_mcp_config

    env.delenv("SAC_TEST_NEVER_SET_ANYWHERE")
    cfg = AgentConfig(
        name="agent-x",
        mcp_servers={"cards": {"command": "scitex-cards"}},
        env={"STORE": "${SAC_TEST_NEVER_SET_ANYWHERE}"},
    )
    # Act
    raised = pytest.raises(UnexpandedEnvValueError)
    # Assert
    with raised:
        setup_mcp_config(cfg, str(tmp_path))
