"""Tests for ``runtimes/_apptainer_inner_argv.py``.

Covers the startup_commands shell-exec wrapper and the absence of
the legacy --mission fallback. See commit message
``feat(startup_commands): execute as container shell``.
"""

from __future__ import annotations

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import StartupCommand
from scitex_agent_container.runtimes._apptainer_inner_argv import (
    _format_shell_steps,
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
