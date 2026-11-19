"""Sealed TUI panes start the agent tree under the control-plane mask.

``build_inner_argv(tui=True)`` for a ``spec.sandbox`` config: Claude Code
and Codex panes ARE the mask (startup_commands + harness inside it); Hermes
and OpenCode owners mask the harness themselves and get their
startup_commands in a separate masked shell. Unsealed panes are unchanged.
"""

from __future__ import annotations

import shlex

import pytest

from scitex_agent_container._sandbox._layout import C_HIDE
from scitex_agent_container._sandbox._mask import ISOLATED_MODULE_BOOTSTRAP
from scitex_agent_container._sandbox._spec import parse_sandbox
from scitex_agent_container.config import AgentConfig, ClaudeSpec, ProviderSpec
from scitex_agent_container.config._types import StartupCommand
from scitex_agent_container.runtimes._apptainer_inner_argv import (
    _GIT_ENV_ALIAS_STEPS,
    _format_shell_steps,
    build_inner_argv,
)
from scitex_agent_container.runtimes._apptainer_inner_argv_tui import (
    _tui_runner_argv,
)
from scitex_agent_container.runtimes._gateway_opencode import OPENCODE_GATEWAY

STARTUP = [StartupCommand(command="touch /work/startup-ran")]
MASK = ["python3", "-I", C_HIDE]


def _sandbox():
    return parse_sandbox(
        {
            "sandbox": {
                "egress": {"allow": ["pypi.org:443"]},
                "env": {"allow": [], "model_key": "K"},
                "run": {"root": "/scratch/unused", "size": "1G", "after": "remove"},
            }
        }
    )


def _cfg(harness: str = "anthropic", *, sealed: bool = True) -> AgentConfig:
    provider = ProviderSpec(
        base_url="http://127.0.0.1:9/", auth_token_env="PROBE_MODEL_KEY"
    )
    cfg = AgentConfig(
        name="sealed-pane",
        runtime="tui",
        harness=harness,
        sandbox=_sandbox() if sealed else None,
        claude=ClaudeSpec(model="probe-model", provider=provider),
    )
    cfg.startup_commands = list(STARTUP)
    cfg.model = "probe-model"
    cfg.engine_key = "probe-engine"
    cfg.opencode_serve_port = 4999
    return cfg


def test_sealed_claude_pane_starts_under_the_mask():
    # Arrange
    cfg = _cfg()
    # Act
    argv = build_inner_argv(cfg, tui=True)
    # Assert
    assert argv[:3] == MASK


def test_sealed_claude_startup_commands_run_inside_the_mask():
    # Arrange
    cfg = _cfg()
    # Act
    argv = build_inner_argv(cfg, tui=True)
    # Assert
    assert "set -e; touch /work/startup-ran; exec claude --model" in argv[-1]


def test_sealed_codex_pane_starts_under_the_mask():
    # Arrange
    cfg = _cfg("codex")
    # Act
    argv = build_inner_argv(cfg, tui=True)
    # Assert
    assert (argv[:3], "touch /work/startup-ran; exec " in argv[-1]) == (MASK, True)


def test_unsealed_claude_pane_argv_is_unchanged():
    # Arrange
    cfg = _cfg(sealed=False)
    runner = " ".join(shlex.quote(p) for p in _tui_runner_argv(cfg))
    steps = _GIT_ENV_ALIAS_STEPS + _format_shell_steps(STARTUP)
    # Act
    argv = build_inner_argv(cfg, tui=True)
    # Assert
    assert argv == ["/bin/bash", "-lc", "; ".join([*steps, f"exec {runner}"])]


def test_sealed_pane_refuses_the_sac_channel_subscriber():
    # Arrange
    cfg = _cfg()
    # Act
    # Assert
    with pytest.raises(ValueError, match="hides SAC"):
        build_inner_argv(cfg, tui=True, tui_channel_mcp='{"mcpServers": {}}')


def test_sealed_opencode_owner_runs_isolated_from_the_launching_sac():
    # Arrange
    cfg = _cfg("opencode")
    # Act
    inline = build_inner_argv(cfg, tui=True)[2]
    # Assert
    assert shlex.quote(ISOLATED_MODULE_BOOTSTRAP) in inline


def test_sealed_opencode_owner_hides_the_state_dir_from_serve_and_tui():
    # Arrange
    cfg = _cfg("opencode")
    # Act
    inline = build_inner_argv(cfg, tui=True)[2]
    # Assert
    assert "--hide /state/sealed-pane" in inline.split("exec ", 1)[1]


def test_sealed_opencode_startup_commands_run_masked_before_the_owner():
    # Arrange
    cfg = _cfg("opencode")
    # Act
    inline = build_inner_argv(cfg, tui=True)[2]
    # Assert
    assert (
        inline.index(f"python3 -I {C_HIDE}")
        < inline.index("touch")
        < inline.index("exec /usr/bin/tini")
    )


def test_unsealed_opencode_owner_argv_is_unchanged():
    # Arrange
    cfg = _cfg("opencode", sealed=False)
    owner = OPENCODE_GATEWAY.owner_argv(
        cfg, __import__("pathlib").Path("/state/sealed-pane")
    )
    # Act
    inline = build_inner_argv(cfg, tui=True)[2]
    # Assert
    assert inline.endswith("exec " + " ".join(shlex.quote(p) for p in owner))


def test_sealed_hermes_owner_runs_isolated_from_the_launching_sac():
    # Arrange
    cfg = _cfg("hermes")
    # Act
    inline = build_inner_argv(cfg, tui=True)[2]
    # Assert
    assert " -m scitex_agent_container" not in inline
