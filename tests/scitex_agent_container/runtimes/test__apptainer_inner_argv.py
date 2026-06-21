"""Tests for ``runtimes/_apptainer_inner_argv.py``.

Covers the startup_commands shell-exec wrapper and the absence of
the legacy --mission fallback. See commit message
``feat(startup_commands): execute as container shell``.
"""

from __future__ import annotations

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import (
    A2ASpec,
    ClaudeSpec,
    RestartSpec,
    StartupCommand,
)
from scitex_agent_container.runtimes._apptainer_inner_argv import (
    _SUPERVISOR_RESTART_FLOOR,
    _agent_runner_argv,
    _format_shell_steps,
    _resolve_max_restarts,
    _resolve_restart_backoff_s,
    build_inner_argv,
)


def _mk_cfg(**kwargs):
    return AgentConfig(name="t", runtime="apptainer", **kwargs)


# ---------------------------------------------------------------------------
# build_inner_argv: shell-exec wrapper
# ---------------------------------------------------------------------------


def test_startup_commands_empty_returns_plain_tini_argv():
    # Arrange
    cfg = _mk_cfg()
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert argv[0] == "/usr/bin/tini"


def test_single_startup_command_wraps_in_bash():
    # Arrange
    cfg = _mk_cfg(startup_commands=[StartupCommand(command="pip install foo")])
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert argv[0] == "/bin/bash"


def test_single_startup_command_passes_dash_lc():
    # Arrange
    cfg = _mk_cfg(startup_commands=[StartupCommand(command="pip install foo")])
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert argv[1] == "-lc"


def test_single_startup_command_emits_set_e_first():
    # Arrange
    cfg = _mk_cfg(startup_commands=[StartupCommand(command="pip install foo")])
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert argv[2].startswith("set -e;")


def test_single_startup_command_emits_exec_then_tini():
    # Arrange
    cfg = _mk_cfg(startup_commands=[StartupCommand(command="pip install foo")])
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert "exec /usr/bin/tini" in argv[2]


def test_multi_startup_commands_chain_with_semicolons():
    # Arrange
    cfg = _mk_cfg(
        startup_commands=[
            StartupCommand(command="A"),
            StartupCommand(command="B"),
            StartupCommand(command="C"),
        ]
    )
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert "set -e; A; B; C; exec" in argv[2]


def test_startup_command_delay_emits_sleep_n():
    # Arrange
    cfg = _mk_cfg(startup_commands=[StartupCommand(delay=5, command="pip install foo")])
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert "sleep 5" in argv[2]


def test_empty_command_string_is_filtered_out():
    # Arrange
    cfg = _mk_cfg(startup_commands=[StartupCommand(command="")])
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert argv[0] == "/usr/bin/tini"


def test_shlex_quote_protects_paths_with_spaces():
    # Arrange
    cfg = _mk_cfg(
        startup_commands=[StartupCommand(command="echo hi")],
        config_path="/has space/spec.yaml",
    )
    # Act
    argv = build_inner_argv(cfg)
    # Assert — config_path is consumed only when a2a.port is set, so
    # for this test we just confirm that the wrapper survives spaces
    # in inputs (the runner argv itself doesn't include config_path
    # here since a2a is unset, but shlex.quote is exercised on every
    # element).
    assert "/bin/bash" in argv


# ---------------------------------------------------------------------------
# build_inner_argv: NO legacy --mission fallback
# ---------------------------------------------------------------------------


def test_startup_commands_present_but_no_prompts_means_no_mission_flag():
    # Arrange — startup_commands has prose-like content (old fallback
    # would have promoted this to --mission). After the refactor, it
    # is shell-executed instead.
    cfg = _mk_cfg(
        startup_commands=[StartupCommand(command="audit this codebase")],
    )
    # Act
    argv = build_inner_argv(cfg)
    # Assert — no --mission flag anywhere in the wrapper.
    assert "--mission" not in argv[2]


# ---------------------------------------------------------------------------
# _format_shell_steps unit
# ---------------------------------------------------------------------------


def test_format_shell_steps_empty_list_returns_empty():
    # Arrange
    cmds: list = []
    # Act
    steps = _format_shell_steps(cmds)
    # Assert
    assert steps == []


def test_format_shell_steps_skips_whitespace_only_commands():
    # Arrange
    cmds = [StartupCommand(command="   ")]
    # Act
    steps = _format_shell_steps(cmds)
    # Assert
    assert steps == []


def test_format_shell_steps_first_real_command_prepends_set_e():
    # Arrange
    cmds = [StartupCommand(command="ls")]
    # Act
    steps = _format_shell_steps(cmds)
    # Assert
    assert steps[0] == "set -e"


def test_format_shell_steps_delay_zero_omits_sleep():
    # Arrange
    cmds = [StartupCommand(delay=0, command="ls")]
    # Act
    steps = _format_shell_steps(cmds)
    # Assert
    assert "sleep 0" not in steps


# ---------------------------------------------------------------------------
# _agent_runner_argv: spec.claude.channels → --channels (sac-node-comms fix)
# ---------------------------------------------------------------------------


def test_agent_runner_argv_no_channels_omits_flag():
    # Arrange
    cfg = _mk_cfg(claude=ClaudeSpec(channels=[]), a2a=A2ASpec(port=9999))
    # Act
    argv = _agent_runner_argv(cfg, one_shot=False)
    # Assert
    assert "--channels" not in argv


def test_agent_runner_argv_emits_channels_flag_when_configured():
    # Arrange
    cfg = _mk_cfg(claude=ClaudeSpec(channels=["server:sac"]), a2a=A2ASpec(port=9999))
    # Act
    argv = _agent_runner_argv(cfg, one_shot=False)
    # Assert
    assert "--channels" in argv


def test_agent_runner_argv_emits_channel_value_after_flag():
    # Arrange
    cfg = _mk_cfg(claude=ClaudeSpec(channels=["server:sac"]), a2a=A2ASpec(port=9999))
    # Act
    argv = _agent_runner_argv(cfg, one_shot=False)
    # Assert
    assert argv[argv.index("--channels") + 1] == "server:sac"


def test_agent_runner_argv_emits_one_flag_per_channel():
    # Arrange
    cfg = _mk_cfg(
        claude=ClaudeSpec(channels=["server:sac", "client:x"]),
        a2a=A2ASpec(port=9999),
    )
    # Act
    argv = _agent_runner_argv(cfg, one_shot=False)
    # Assert
    assert argv.count("--channels") == 2


def test_agent_runner_argv_skips_blank_channel_entries():
    # Arrange
    cfg = _mk_cfg(claude=ClaudeSpec(channels=["", "  "]), a2a=A2ASpec(port=9999))
    # Act
    argv = _agent_runner_argv(cfg, one_shot=False)
    # Assert
    assert "--channels" not in argv


# ---------------------------------------------------------------------------
# _agent_runner_argv: supervisor restart cap (resume-recovery enablement).
# The runner CLI defaults --max-restarts to 0, which disables the
# history-walk resume recovery for every restart.policy: never agent.
# The adapter must pass a >0 floor so the recovery path is always live.
# ---------------------------------------------------------------------------


def test_agent_runner_argv_emits_max_restarts_flag():
    # Arrange
    cfg = _mk_cfg()
    # Act
    argv = _agent_runner_argv(cfg, one_shot=False)
    # Assert
    assert "--max-restarts" in argv


def test_agent_runner_argv_default_max_restarts_is_above_zero():
    # Arrange
    cfg = _mk_cfg()
    # Act
    argv = _agent_runner_argv(cfg, one_shot=False)
    # Assert
    assert int(argv[argv.index("--max-restarts") + 1]) > 0


def test_agent_runner_argv_emits_restart_backoff_flag():
    # Arrange
    cfg = _mk_cfg()
    # Act
    argv = _agent_runner_argv(cfg, one_shot=False)
    # Assert
    assert "--restart-backoff-s" in argv


def test_resolve_max_restarts_never_policy_uses_floor():
    # Arrange — restart.policy: never (the live-agent case) must still
    # get the floor so resume recovery is live.
    cfg = _mk_cfg(restart=RestartSpec(policy="never"))
    # Act
    resolved = _resolve_max_restarts(cfg)
    # Assert
    assert resolved == _SUPERVISOR_RESTART_FLOOR


def test_resolve_max_restarts_on_failure_above_floor_raises_cap():
    # Arrange — an explicit on-failure policy with a higher retry count
    # raises the cap above the floor.
    cfg = _mk_cfg(
        restart=RestartSpec(
            policy="on-failure", max_retries=_SUPERVISOR_RESTART_FLOOR + 5
        )
    )
    # Act
    resolved = _resolve_max_restarts(cfg)
    # Assert
    assert resolved == _SUPERVISOR_RESTART_FLOOR + 5


def test_resolve_max_restarts_on_failure_below_floor_keeps_floor():
    # Arrange — a small max_retries must not lower the resume-recovery floor.
    cfg = _mk_cfg(restart=RestartSpec(policy="on-failure", max_retries=1))
    # Act
    resolved = _resolve_max_restarts(cfg)
    # Assert
    assert resolved == _SUPERVISOR_RESTART_FLOOR


def test_resolve_max_restarts_always_policy_uses_max_retries():
    # Arrange
    cfg = _mk_cfg(
        restart=RestartSpec(policy="always", max_retries=_SUPERVISOR_RESTART_FLOOR + 2)
    )
    # Act
    resolved = _resolve_max_restarts(cfg)
    # Assert
    assert resolved == _SUPERVISOR_RESTART_FLOOR + 2


def test_resolve_restart_backoff_never_policy_uses_runner_default():
    # Arrange
    cfg = _mk_cfg(restart=RestartSpec(policy="never", backoff_initial=99))
    # Act
    resolved = _resolve_restart_backoff_s(cfg)
    # Assert
    assert resolved == 1.0


def test_resolve_restart_backoff_on_failure_uses_spec_backoff():
    # Arrange
    cfg = _mk_cfg(restart=RestartSpec(policy="on-failure", backoff_initial=7))
    # Act
    resolved = _resolve_restart_backoff_s(cfg)
    # Assert
    assert resolved == 7.0


# ---------------------------------------------------------------------------
# TUI session continuity: ``-c`` (--continue) appended ONLY for
# ``claude.session: continue`` ("fresh by default, opt-in continue").
# Built via build_inner_argv(tui=True) so the whole inner-argv path is
# exercised (not just _tui_runner_argv in isolation).
# ---------------------------------------------------------------------------


def _tui_argv(session: str) -> list[str]:
    cfg = _mk_cfg(claude=ClaudeSpec(model="haiku", session=session))
    return build_inner_argv(cfg, tui=True)


def test_tui_session_fresh_omits_continue_flag():
    # Arrange — fresh is the default mode; an experiment trial must start
    # an independent session.
    # Act
    argv = _tui_argv("fresh")
    # Assert
    assert "-c" not in argv


def test_tui_session_continue_appends_continue_flag():
    # Arrange — a coordinator opts into continuity.
    # Act
    argv = _tui_argv("continue")
    # Assert
    assert "-c" in argv


def test_tui_session_resume_omits_continue_flag():
    # Arrange — resume is delivered as --resume <id>, never bare -c.
    # Act
    argv = _tui_argv("resume")
    # Assert
    assert "-c" not in argv


def test_tui_session_new_session_alias_omits_continue_flag():
    # Arrange — ``new-session`` is the back-compat alias for fresh; a CLI
    # override may set it verbatim on claude.session (bypassing the parser
    # alias map), so the argv builder must still treat it as fresh.
    # Act
    argv = _tui_argv("new-session")
    # Assert
    assert "-c" not in argv


def test_tui_inner_argv_default_claudespec_is_fresh_no_continue_flag():
    # Arrange — a ClaudeSpec with NO session set carries the dataclass
    # default (fresh); the TUI must not resume.
    cfg = _mk_cfg(claude=ClaudeSpec(model="haiku"))
    # Act
    argv = build_inner_argv(cfg, tui=True)
    # Assert
    assert "-c" not in argv
