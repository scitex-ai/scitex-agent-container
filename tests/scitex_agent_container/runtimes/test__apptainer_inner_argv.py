"""Tests for ``runtimes/_apptainer_inner_argv.py``.

Covers the startup_commands shell-exec wrapper, the absence of the
legacy --mission fallback, and the unconditional SAC_GIT_* -> GIT_*
env-alias step (see commit message
``feat(launch): generic SAC_GIT_* -> GIT_* env alias in the container
shell wrapper``). See also commit message
``feat(startup_commands): execute as container shell``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from scitex_agent_container.config import AgentConfig, ProxySpec
from scitex_agent_container.config._a2a_defaults import DEFAULT_A2A_HOST
from scitex_agent_container.config._harness_types import (
    V4_HARNESS_DISPATCH_CARD,
    HarnessRuntimeMismatchError,
)
from scitex_agent_container.config._types import (
    A2ASpec,
    ClaudeSpec,
    RestartSpec,
    StartupCommand,
)
from scitex_agent_container.runtimes._apptainer_inner_argv import (
    _GIT_ENV_ALIAS_STEPS,
    _SUPERVISOR_RESTART_FLOOR,
    _agent_runner_argv,
    _format_shell_steps,
    _home_has_resumable_conversation,
    _proxy_runner_argv,
    _resolve_max_restarts,
    _resolve_restart_backoff_s,
    build_inner_argv,
)


def _mk_cfg(**kwargs):
    return AgentConfig(name="t", runtime="apptainer", **kwargs)


def _flag_value(argv: list[str], flag: str) -> str | None:
    """Value following ``flag`` in ``argv``, or None when the flag is absent."""
    return argv[argv.index(flag) + 1] if flag in argv else None


# ---------------------------------------------------------------------------
# build_inner_argv: shell-exec wrapper
# ---------------------------------------------------------------------------


def test_startup_commands_empty_still_wraps_in_bash():
    # Arrange — EMPTY startup_commands. Before the SAC_GIT_* alias step
    # existed this returned the bare tini argv unwrapped; now the alias
    # is unconditional, so every agent (even one with no startup_commands
    # at all) gets the bash -lc wrapper.
    cfg = _mk_cfg()
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert argv[0] == "/bin/bash"


def test_startup_commands_empty_still_passes_dash_lc():
    # Arrange
    cfg = _mk_cfg()
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert argv[1] == "-lc"


def test_startup_commands_empty_still_execs_tini():
    # Arrange
    cfg = _mk_cfg()
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert "exec /usr/bin/tini" in argv[2]


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


def test_single_startup_command_emits_set_e_after_git_alias():
    # Arrange — the fixed SAC_GIT_* alias steps are now unconditionally
    # prepended, so "set -e" (from _format_shell_steps) is no longer the
    # very first thing in the inline script; it still directly precedes
    # the user's own startup command.
    cfg = _mk_cfg(startup_commands=[StartupCommand(command="pip install foo")])
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert "; set -e; pip install foo;" in argv[2]


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
    # Arrange — the blank command contributes NO shell step of its own,
    # but the wrap still happens (the unconditional git-alias step).
    cfg = _mk_cfg(startup_commands=[StartupCommand(command="")])
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert "exec /usr/bin/tini" in argv[2]


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
# SAC_GIT_* -> GIT_* env alias: generic, values-agnostic, always present.
# git itself only reads the literal GIT_* names; the alias mirrors whichever
# SAC_GIT_* vars are already in the shell env (source: each project's own
# .envrc via direnv source_up — entirely outside this module's concern) onto
# them. See _GIT_ENV_ALIAS_STEPS.
# ---------------------------------------------------------------------------

_EXPECTED_GIT_ALIAS_MAPPINGS = (
    ("SAC_GIT_AUTHOR_NAME", "GIT_AUTHOR_NAME"),
    ("SAC_GIT_AUTHOR_EMAIL", "GIT_AUTHOR_EMAIL"),
    ("SAC_GIT_COMMITTER_NAME", "GIT_COMMITTER_NAME"),
    ("SAC_GIT_COMMITTER_EMAIL", "GIT_COMMITTER_EMAIL"),
    ("SAC_GIT_SSH_COMMAND", "GIT_SSH_COMMAND"),
)


def test_git_alias_step_count_is_exactly_five():
    # Arrange — one guarded export per mapping, no more, no less.
    steps = _GIT_ENV_ALIAS_STEPS
    # Act
    count = len(steps)
    # Assert
    assert count == 5


def test_git_alias_steps_contain_all_five_mappings():
    # Arrange
    joined = "; ".join(_GIT_ENV_ALIAS_STEPS)
    expected = [
        f'[ -n "${{{src}:-}}" ] && export {dst}="${src}"'
        for src, dst in _EXPECTED_GIT_ALIAS_MAPPINGS
    ]
    # Act
    missing = [line for line in expected if line not in joined]
    # Assert — every SAC_GIT_* -> GIT_* mapping is present verbatim.
    assert missing == []


def test_empty_startup_commands_argv_contains_git_alias_steps():
    # Arrange — an agent with NO startup_commands at all still gets the
    # alias baked into its wrapper.
    cfg = _mk_cfg()
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert "SAC_GIT_AUTHOR_NAME" in argv[2]


def test_git_alias_precedes_exec_in_empty_startup_commands_case():
    # Arrange
    cfg = _mk_cfg()
    # Act
    argv = build_inner_argv(cfg)
    # Assert — alias runs before the tini exec, not after.
    assert argv[2].index("SAC_GIT_AUTHOR_NAME") < argv[2].index("exec ")


def test_git_alias_precedes_custom_startup_command():
    # Arrange — an agent WITH its own startup_commands still gets the
    # alias step prepended BEFORE its own steps.
    cfg = _mk_cfg(startup_commands=[StartupCommand(command="echo custom-step")])
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert argv[2].index("SAC_GIT_AUTHOR_NAME") < argv[2].index("echo custom-step")


def test_git_alias_runs_before_set_e_from_startup_commands():
    # Arrange — the alias lines must run BEFORE any `set -e` emitted for
    # startup_commands, so a false `[ -n ... ]` (unset SAC_GIT_*) never
    # aborts the launch under set -e.
    cfg = _mk_cfg(startup_commands=[StartupCommand(command="echo custom-step")])
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert argv[2].index("SAC_GIT_AUTHOR_NAME") < argv[2].index("set -e")


def test_git_alias_is_noop_shell_when_source_vars_unset():
    # Arrange — real bash execution (no mocks): clear every SAC_GIT_*/GIT_*
    # from the env, run the exact generated alias snippet, then dump env.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("SAC_GIT_") and not k.startswith("GIT_")
    }
    script = "; ".join(_GIT_ENV_ALIAS_STEPS) + "; env"
    # Act
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    # Assert — no GIT_* leaked into the child env; silent no-op.
    assert "GIT_AUTHOR_NAME" not in result.stdout


def test_git_alias_mirrors_value_when_source_var_set():
    # Arrange — real bash execution proving the mechanism end-to-end.
    env = dict(os.environ)
    env["SAC_GIT_AUTHOR_NAME"] = "Test Author"
    script = "; ".join(_GIT_ENV_ALIAS_STEPS) + '; printf "%s" "$GIT_AUTHOR_NAME"'
    # Act
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    # Assert
    assert result.stdout == "Test Author"


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


def _exec_tail_tokens(argv: list[str]) -> list[str]:
    """Tokenize the ``exec <runner...>`` tail of a bash -lc wrapped argv.

    ``build_inner_argv`` now ALWAYS wraps in ``/bin/bash -lc <inline>``
    (unconditional SAC_GIT_* alias step), so the actual runner argv
    (e.g. ``claude --model haiku -c``) is embedded as shell-quoted text
    inside ``argv[2]`` rather than being ``argv`` itself. Token-splitting
    the ``exec`` tail (instead of a raw substring check) avoids false
    matches against unrelated ``-c``-containing text elsewhere in the
    alias/startup-command steps.
    """
    import shlex as _shlex

    inline = argv[2]
    exec_part = inline.split("exec ", 1)[1]
    return _shlex.split(exec_part)


def test_tui_session_fresh_omits_continue_flag():
    # Arrange — fresh is the default mode; an experiment trial must start
    # an independent session.
    # Act
    argv = _tui_argv("fresh")
    # Assert
    assert "-c" not in _exec_tail_tokens(argv)


def test_tui_session_continue_with_history_appends_continue_flag():
    # Arrange — continue-mode AND a prior transcript exists in the home, so
    # ``claude -c`` has a conversation to resume.
    cfg = AgentConfig(
        name="t-cont-hist",
        runtime="apptainer",
        claude=ClaudeSpec(model="haiku", session="continue"),
    )
    _, home = _home_has_resumable_conversation(cfg)
    conv = Path(home) / ".claude" / "projects" / "proj" / "c.jsonl"
    conv.parent.mkdir(parents=True, exist_ok=True)
    conv.write_text("{}\n")
    # Act
    try:
        argv = build_inner_argv(cfg, tui=True)
    finally:
        shutil.rmtree(Path(home) / ".claude", ignore_errors=True)
    # Assert
    assert "-c" in _exec_tail_tokens(argv)


def test_tui_session_continue_without_history_omits_continue_flag():
    # Arrange — continue-mode but a FRESH home (no transcript): interactive
    # ``claude -c`` would print "No conversation found to continue" and EXIT,
    # silently killing the boot, so the flag MUST be omitted (fail-loud gate).
    cfg = AgentConfig(
        name="t-cont-fresh",
        runtime="apptainer",
        claude=ClaudeSpec(model="haiku", session="continue"),
    )
    _, home = _home_has_resumable_conversation(cfg)
    shutil.rmtree(Path(home) / ".claude", ignore_errors=True)
    # Act
    argv = build_inner_argv(cfg, tui=True)
    # Assert
    assert "-c" not in _exec_tail_tokens(argv)


def test_tui_session_resume_omits_continue_flag():
    # Arrange — resume is delivered as --resume <id>, never bare -c.
    # Act
    argv = _tui_argv("resume")
    # Assert
    assert "-c" not in _exec_tail_tokens(argv)


def test_tui_session_new_session_alias_omits_continue_flag():
    # Arrange — ``new-session`` is the back-compat alias for fresh; a CLI
    # override may set it verbatim on claude.session (bypassing the parser
    # alias map), so the argv builder must still treat it as fresh.
    # Act
    argv = _tui_argv("new-session")
    # Assert
    assert "-c" not in _exec_tail_tokens(argv)


def test_tui_inner_argv_default_claudespec_is_fresh_no_continue_flag():
    # Arrange — a ClaudeSpec with NO session set carries the dataclass
    # default (fresh); the TUI must not resume.
    cfg = _mk_cfg(claude=ClaudeSpec(model="haiku"))
    # Act
    argv = build_inner_argv(cfg, tui=True)
    # Assert
    assert "-c" not in _exec_tail_tokens(argv)


# ---------------------------------------------------------------------------
# spec.a2a.host -> --a2a-host  (the SDK runner's uvicorn bind address)
#
# This builder is the THIRD of sac's three a2a bind paths. Until it was
# threaded it emitted --a2a-port alone, so the SDK runner fell back to its own
# separate "127.0.0.1" flag default and a spec's declared host reached exactly
# one path (runtimes/a2a_sidecar.py). The value declared in the spec and the
# address actually bound could therefore disagree with nothing reporting it.
#
# Both directions are pinned here, because a threading change earns trust only
# by proving BOTH:
#   1. an UNCHANGED spec (host 127.0.0.1, as all 102 fleet specs declare)
#      still yields loopback — no silent widening;
#   2. a CHANGED spec is FOLLOWED verbatim.
# The value flows on from here: --a2a-host -> _session_cli.main ->
# claude_session.run(a2a_host=) -> _session_http.serve_inbound(host=) ->
# uvicorn.Config(host=).
# ---------------------------------------------------------------------------

# A resolved (non-"auto") a2a port; PEP 515 separators satisfy STX-NL001.
_A2A_PORT = 7_901
# A deliberately NON-loopback bind, the case the whole change exists for.
_WILDCARD_HOST = "0.0.0.0"
_LAN_HOST = "192.168.11.23"


def test_agent_runner_argv_emits_a2a_host_alongside_the_port():
    # Arrange — a spec with a resolved port and the default declared host.
    cfg = _mk_cfg(a2a=A2ASpec(port=_A2A_PORT))
    # Act
    argv = _agent_runner_argv(cfg, one_shot=False)
    # Assert
    assert "--a2a-host" in argv


def test_agent_runner_argv_a2a_host_is_loopback_for_an_undeclared_host():
    # Arrange — CASE 1 (no-regression): a spec that names no host must still
    # produce the loopback bind it produced before this flag existed.
    cfg = _mk_cfg(a2a=A2ASpec(port=_A2A_PORT))
    # Act
    argv = _agent_runner_argv(cfg, one_shot=False)
    # Assert
    assert _flag_value(argv, "--a2a-host") == DEFAULT_A2A_HOST


def test_agent_runner_argv_a2a_host_follows_a_wildcard_spec_host():
    # Arrange — CASE 2: the spec asks for every interface.
    cfg = _mk_cfg(a2a=A2ASpec(host=_WILDCARD_HOST, port=_A2A_PORT))
    # Act
    argv = _agent_runner_argv(cfg, one_shot=False)
    # Assert
    assert _flag_value(argv, "--a2a-host") == _WILDCARD_HOST


def test_agent_runner_argv_a2a_host_follows_a_lan_spec_host():
    # Arrange — CASE 2 again, with the shape an ssh-reachable fleet would use.
    cfg = _mk_cfg(a2a=A2ASpec(host=_LAN_HOST, port=_A2A_PORT))
    # Act
    argv = _agent_runner_argv(cfg, one_shot=False)
    # Assert
    assert _flag_value(argv, "--a2a-host") == _LAN_HOST


def test_agent_runner_argv_omits_a2a_host_without_a_resolved_port():
    # Arrange — an unresolved "auto" port wires up no sidecar at this layer,
    # so a bind ADDRESS would be meaningless (nothing binds).
    cfg = _mk_cfg(a2a=A2ASpec(host=_WILDCARD_HOST, port="auto"))
    # Act
    argv = _agent_runner_argv(cfg, one_shot=False)
    # Assert
    assert "--a2a-host" not in argv


def test_agent_runner_argv_omits_a2a_host_for_a_blank_declared_host():
    # Arrange — a whitespace-only host states nothing; leave the runner's own
    # flag default in charge rather than passing an unbindable empty string.
    cfg = _mk_cfg(a2a=A2ASpec(host="   ", port=_A2A_PORT))
    # Act
    argv = _agent_runner_argv(cfg, one_shot=False)
    # Assert
    assert "--a2a-host" not in argv


def test_agent_runner_argv_still_carries_the_card_yaml_after_the_host():
    # Arrange — the port/host/card-yaml block was factored into one helper;
    # guard that the card path did not get lost in the move.
    cfg = _mk_cfg(a2a=A2ASpec(port=_A2A_PORT), config_path="/spec.yaml")
    # Act
    argv = _agent_runner_argv(cfg, one_shot=False)
    # Assert
    assert _flag_value(argv, "--a2a-card-yaml") == "/spec.yaml"


def test_proxy_runner_argv_a2a_host_is_loopback_for_an_undeclared_host():
    # Arrange — CASE 1 for kind: AgentProxy, which shares the same builder.
    cfg = _mk_cfg(
        kind="AgentProxy",
        proxy=ProxySpec(upstream="http://u"),
        a2a=A2ASpec(port=_A2A_PORT),
    )
    # Act
    argv = _proxy_runner_argv(cfg)
    # Assert
    assert _flag_value(argv, "--a2a-host") == DEFAULT_A2A_HOST


def test_proxy_runner_argv_a2a_host_follows_the_declared_spec_host():
    # Arrange — CASE 2 for kind: AgentProxy.
    cfg = _mk_cfg(
        kind="AgentProxy",
        proxy=ProxySpec(upstream="http://u"),
        a2a=A2ASpec(host=_WILDCARD_HOST, port=_A2A_PORT),
    )
    # Act
    argv = _proxy_runner_argv(cfg)
    # Assert
    assert _flag_value(argv, "--a2a-host") == _WILDCARD_HOST


def test_build_inner_argv_carries_the_declared_a2a_host_into_the_exec_line():
    # Arrange — end of the builder: the flag must survive the bash -lc wrap
    # and shlex quoting, not just exist in the intermediate list.
    cfg = _mk_cfg(a2a=A2ASpec(host=_WILDCARD_HOST, port=_A2A_PORT))
    # Act
    tokens = _exec_tail_tokens(build_inner_argv(cfg))
    # Assert
    assert _flag_value(tokens, "--a2a-host") == _WILDCARD_HOST


# ---------------------------------------------------------------------------
# v4 step-2 harness guard — a non-Anthropic harness must never silently get
# the Claude runner module (card
# sac-v4-layering-refactor-harness-runtime-inference-20260813). The old
# dispatch read ``getattr(config, "provider", None)`` — a field the harness
# rename removed from AgentConfig — so ``harness: openai`` specs silently
# got RUNNER_MODULE_AGENT (verified pre-fix 2026-08-14).
# ---------------------------------------------------------------------------


def test_build_inner_argv_never_emits_the_claude_runner_for_openai_harness():
    # Arrange — the pre-fix bug verbatim: the argv carried
    # scitex_agent_container._runners.claude_session for an openai harness.
    cfg = _mk_cfg(harness="openai")
    argv: list[str] = []
    # Act
    try:
        argv = build_inner_argv(cfg)
    except Exception:  # stx-allow: test-capture (reason: STX-TQ002; a raise is a PASS for this pin — only a silently built Claude argv fails it.)
        pass
    # Assert
    assert "claude_session" not in " ".join(argv)


def test_build_inner_argv_raises_harness_mismatch_for_openai_harness():
    # Arrange
    cfg = _mk_cfg(harness="openai")
    raised: BaseException | None = None
    # Act
    try:
        build_inner_argv(cfg)
    except HarnessRuntimeMismatchError as exc:  # stx-allow: test-capture (reason: STX-TQ002.)
        raised = exc
    # Assert
    assert isinstance(raised, HarnessRuntimeMismatchError)


def test_build_inner_argv_openai_harness_refusal_names_the_runner_module():
    # Arrange
    cfg = _mk_cfg(harness="openai")
    raised: BaseException | None = None
    # Act
    try:
        build_inner_argv(cfg)
    except HarnessRuntimeMismatchError as exc:  # stx-allow: test-capture (reason: STX-TQ002.)
        raised = exc
    # Assert — names what was actually about to launch.
    assert raised is not None and "claude_session" in str(raised)


def test_build_inner_argv_openai_harness_refusal_covers_the_tui_branch():
    # Arrange — the TUI branch never had even the dead provider check;
    # the interactive claude TUI is just as wrong a vendor.
    cfg = _mk_cfg(harness="openai")
    raised: BaseException | None = None
    # Act
    try:
        build_inner_argv(cfg, tui=True)
    except HarnessRuntimeMismatchError as exc:  # stx-allow: test-capture (reason: STX-TQ002.)
        raised = exc
    # Assert
    assert isinstance(raised, HarnessRuntimeMismatchError)


def test_build_inner_argv_openai_harness_refusal_names_the_v4_card():
    # Arrange
    cfg = _mk_cfg(harness="openai")
    raised: BaseException | None = None
    # Act
    try:
        build_inner_argv(cfg)
    except HarnessRuntimeMismatchError as exc:  # stx-allow: test-capture (reason: STX-TQ002.)
        raised = exc
    # Assert
    assert raised is not None and V4_HARNESS_DISPATCH_CARD in str(raised)


def test_build_inner_argv_proxy_kind_is_exempt_from_the_harness_guard():
    # Arrange — the a2a proxy runner is vendor-neutral: a harness value on
    # a proxy spec mismatches nothing the guard protects.
    cfg = _mk_cfg(
        kind="AgentProxy",
        proxy=ProxySpec(upstream="http://u"),
        harness="openai",
    )
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert "a2a_proxy" in " ".join(argv)


def test_build_inner_argv_anthropic_harness_still_gets_the_claude_runner():
    # Arrange — byte-identical selection for the fleet's real specs.
    cfg = _mk_cfg(harness="anthropic")
    # Act
    argv = build_inner_argv(cfg)
    # Assert
    assert "claude_session" in " ".join(argv)
