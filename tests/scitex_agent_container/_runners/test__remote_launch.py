"""Unit tests for the generic remote-launch script generator.

Exercises ``render_remote_launch`` end-to-end at the string level: the
function is pure (argv + flags in, bash script text out) so each invariant
of the rendered script — shebang, host-hook source line, ``exec`` vs
``setsid nohup`` mode selection, state-root export, shell-quoting of
argv tokens, and ``$SAC_RUNNER_PREFIX`` expansion — is asserted against
the real output.

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007). Same-shape
invariants over a small input matrix collapse into ``pytest.parametrize``
(TQ001). No mocks/monkeypatch — the renderer is pure, so inputs go in
directly and the returned string is the system under test.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._runners._remote_launch import (
    HOST_HOOK_DIR,
    render_remote_launch,
)

# ---------------------------------------------------------------------------
# Canonical runner argv used by most cases — short, realistic, single-token.
# ---------------------------------------------------------------------------

_CANON_ARGV = [
    "python",
    "-m",
    "scitex_agent_container._runners.claude_session",
    "--name",
    "x",
]


class TestForegroundRendering:
    def test_foreground_script_starts_with_bash_shebang(self) -> None:
        # Arrange
        argv = _CANON_ARGV

        # Act
        out = render_remote_launch(runner_argv=argv, agent_name="x", detach=False)

        # Assert
        assert out.startswith("#!/usr/bin/env bash\n")

    def test_foreground_emits_exec_line_with_runner_prefix_and_cmd(self) -> None:
        # Arrange
        argv = _CANON_ARGV

        # Act
        out = render_remote_launch(runner_argv=argv, agent_name="x", detach=False)

        # Assert
        assert (
            "exec ${SAC_RUNNER_PREFIX:-} python -m "
            "scitex_agent_container._runners.claude_session --name x"
        ) in out

    def test_foreground_references_host_hook_directory(self) -> None:
        # Arrange
        argv = _CANON_ARGV

        # Act
        out = render_remote_launch(runner_argv=argv, agent_name="x", detach=False)

        # Assert
        assert HOST_HOOK_DIR in out

    def test_foreground_sources_host_hook_only_if_present(self) -> None:
        # Arrange
        argv = _CANON_ARGV

        # Act
        out = render_remote_launch(runner_argv=argv, agent_name="x", detach=False)

        # Assert
        assert '[ -f "$_sac_hook" ] && . "$_sac_hook"' in out


class TestDetachRendering:
    def test_detach_uses_setsid_nohup_for_session_leader(self) -> None:
        # Arrange
        argv = _CANON_ARGV

        # Act
        out = render_remote_launch(runner_argv=argv, agent_name="y", detach=True)

        # Assert
        assert "setsid nohup" in out

    def test_detach_emits_child_pid_on_stdout_for_caller_capture(self) -> None:
        # Arrange
        argv = _CANON_ARGV

        # Act
        out = render_remote_launch(runner_argv=argv, agent_name="y", detach=True)

        # Assert
        assert out.rstrip().endswith("echo $!")

    def test_detach_logs_to_default_runtime_log_path_under_agent_name(self) -> None:
        # Arrange
        argv = _CANON_ARGV

        # Act
        out = render_remote_launch(runner_argv=argv, agent_name="y", detach=True)

        # Assert
        assert "/runtime/y/runner.log" in out


class TestStateRootExport:
    def test_state_root_is_exported_as_runtime_dir_env_var(self) -> None:
        # Arrange
        argv = ["python", "-m", "scitex_agent_container._runners.claude_session"]

        # Act
        out = render_remote_launch(
            runner_argv=argv,
            agent_name="z",
            state_root="/tmp/sac-remote",
        )

        # Assert
        assert "export SCITEX_AGENT_CONTAINER_RUNTIME_DIR=/tmp/sac-remote" in out


class TestShellQuoting:
    def test_argv_token_with_spaces_and_metachars_is_shell_quoted(self) -> None:
        """argv tokens with spaces must survive transport intact."""
        # Arrange
        argv = [
            "python",
            "-m",
            "scitex_agent_container._runners.claude_session",
            "--mission",
            "hello world; rm -rf /",
        ]

        # Act
        out = render_remote_launch(
            runner_argv=argv,
            agent_name="injection-test",
            detach=False,
        )

        # Assert: the dangerous tokens are shell-quoted, not splatted.
        assert "'hello world; rm -rf /'" in out


class TestHostHookPath:
    def test_hook_path_uses_remote_dollar_hostname_not_literal(self) -> None:
        """Lookup uses the *remote* host's $(hostname), not a literal name."""
        # Arrange
        argv = ["python"]

        # Act
        out = render_remote_launch(runner_argv=argv, agent_name="x", detach=False)

        # Assert
        assert "$(hostname).sh" in out


class TestRunnerPrefixExpansion:
    """``$SAC_RUNNER_PREFIX`` is expanded before the runner cmd so
    per-host hooks can wrap with srun / apptainer / etc."""

    @pytest.mark.parametrize("detach", [True, False])
    def test_runner_prefix_placeholder_present_for_each_detach_mode(
        self, detach: bool
    ) -> None:
        # Arrange
        argv = ["python", "-m", "x"]

        # Act
        out = render_remote_launch(runner_argv=argv, agent_name="x", detach=detach)

        # Assert
        assert "${SAC_RUNNER_PREFIX:-}" in out

    @pytest.mark.parametrize("detach", [True, False])
    def test_runner_prefix_immediately_precedes_runner_cmd_for_each_detach_mode(
        self, detach: bool
    ) -> None:
        # Arrange
        argv = ["python", "-m", "x"]

        # Act
        out = render_remote_launch(runner_argv=argv, agent_name="x", detach=detach)

        # Assert
        assert "${SAC_RUNNER_PREFIX:-} python -m x" in out
