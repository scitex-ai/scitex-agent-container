"""Unit tests for the generic remote-launch script generator."""

from __future__ import annotations

from scitex_agent_container._runners._remote_launch import (
    HOST_HOOK_DIR,
    render_remote_launch,
)


class TestRenderRemoteLaunch:
    def test_foreground_renders_exec(self) -> None:
        out = render_remote_launch(
            runner_argv=[
                "python",
                "-m",
                "scitex_agent_container._runners.claude_session",
                "--name",
                "x",
            ],
            agent_name="x",
            detach=False,
        )
        assert out.startswith("#!/usr/bin/env bash\n")
        assert (
            "exec python -m scitex_agent_container._runners.claude_session --name x"
            in out
        )
        # Per-host hook source is always rendered (silent skip if absent).
        assert HOST_HOOK_DIR in out
        assert '[ -f "$_sac_hook" ] && . "$_sac_hook"' in out

    def test_detach_uses_setsid_and_emits_pid(self) -> None:
        out = render_remote_launch(
            runner_argv=[
                "python",
                "-m",
                "scitex_agent_container._runners.claude_session",
                "--name",
                "y",
            ],
            agent_name="y",
            detach=True,
        )
        assert "setsid nohup" in out
        # PID is emitted on stdout so the caller can record it locally.
        assert out.rstrip().endswith("echo $!")
        # Default log path under runtime dir.
        assert "/runtime/y/runner.log" in out

    def test_state_root_exported(self) -> None:
        out = render_remote_launch(
            runner_argv=[
                "python",
                "-m",
                "scitex_agent_container._runners.claude_session",
            ],
            agent_name="z",
            state_root="/tmp/sac-remote",
        )
        assert "export SCITEX_AGENT_CONTAINER_RUNTIME_DIR=/tmp/sac-remote" in out

    def test_argv_is_shell_quoted(self) -> None:
        """argv tokens with spaces must survive transport intact."""
        out = render_remote_launch(
            runner_argv=[
                "python",
                "-m",
                "scitex_agent_container._runners.claude_session",
                "--mission",
                "hello world; rm -rf /",
            ],
            agent_name="injection-test",
            detach=False,
        )
        # The dangerous tokens must be shell-quoted, not splatted.
        assert "'hello world; rm -rf /'" in out

    def test_per_host_hook_path_is_dollar_hostname(self) -> None:
        """Lookup uses the *remote* host's $(hostname), not a literal name."""
        out = render_remote_launch(
            runner_argv=["python"],
            agent_name="x",
            detach=False,
        )
        assert "$(hostname).sh" in out
