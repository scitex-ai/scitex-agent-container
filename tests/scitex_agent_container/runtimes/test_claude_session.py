"""Tests for ``runtimes/claude_session.py`` adapter (Phase 1)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scitex_agent_container._runners import claude_session as runner
from scitex_agent_container.runtimes.claude_session import (
    ClaudeSessionRuntime,
    _pid_alive,
)


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch) -> Path:
    """Redirect the runner's default state root into tmp_path.

    Two sources of truth post-split (2026-05-03): the runner re-exports
    DEFAULT_STATE_ROOT from _session_state and ``state_dir_for`` reads
    the latter at call time. Patch both.
    """
    from scitex_agent_container._runners import _session_state

    monkeypatch.setattr(runner, "DEFAULT_STATE_ROOT", tmp_path)
    monkeypatch.setattr(_session_state, "DEFAULT_STATE_ROOT", tmp_path)
    return tmp_path


def _config(name: str = "alpha", *, startup_commands=None) -> SimpleNamespace:
    return SimpleNamespace(name=name, startup_commands=startup_commands or [])


def _startup(command: str, delay: int = 0) -> SimpleNamespace:
    return SimpleNamespace(command=command, delay=delay)


# ---------------------------------------------------------------------------
# Synthetic-PID tests (no real subprocess; exercise control flow)
# ---------------------------------------------------------------------------


class TestIsRunning:
    def test_no_pid_file_means_not_running(self, state_root: Path) -> None:
        rt = ClaudeSessionRuntime()
        assert rt.is_running(_config()) is False  # type: ignore[arg-type]

    def test_dead_pid_means_not_running(self, state_root: Path) -> None:
        runner.write_pid(state_root / "alpha", 999_999_999)
        rt = ClaudeSessionRuntime()
        assert rt.is_running(_config()) is False  # type: ignore[arg-type]

    def test_self_pid_counts_as_running(self, state_root: Path) -> None:
        runner.write_pid(state_root / "alpha", os.getpid())
        rt = ClaudeSessionRuntime()
        assert rt.is_running(_config()) is True  # type: ignore[arg-type]


class TestStop:
    def test_no_pid_returns_true(self, state_root: Path) -> None:
        rt = ClaudeSessionRuntime()
        assert rt.stop(_config()) is True  # type: ignore[arg-type]

    def test_dead_pid_cleans_state(self, state_root: Path) -> None:
        sd = state_root / "alpha"
        runner.write_pid(sd, 999_999_999)
        runner.write_heartbeat(sd, pid=999_999_999, state=runner.STATE_IDLE)
        rt = ClaudeSessionRuntime()
        with patch("os.kill", side_effect=ProcessLookupError):
            assert rt.stop(_config()) is True  # type: ignore[arg-type]
        assert not (sd / "pid").exists()
        assert not (sd / "heartbeat.json").exists()


class TestLogs:
    def test_no_heartbeat_yields_placeholder(self, state_root: Path) -> None:
        rt = ClaudeSessionRuntime()
        out = rt.logs(_config())  # type: ignore[arg-type]
        assert "no heartbeat" in out.lower()

    def test_heartbeat_renders_as_json(self, state_root: Path) -> None:
        runner.write_heartbeat(state_root / "alpha", pid=1, state=runner.STATE_IDLE)
        rt = ClaudeSessionRuntime()
        out = rt.logs(_config())  # type: ignore[arg-type]
        assert "idle" in out

    def test_session_jsonl_renders_human_view(self, state_root: Path) -> None:
        sd = state_root / "alpha"
        runner.append_session_message(sd, {"type": "user", "text": "do X"})
        runner.append_session_message(sd, {"type": "assistant", "text": "doing"})
        runner.append_session_message(
            sd,
            {
                "type": "result",
                "session_id": "sess-1",
                "usage": {"input_tokens": 3, "output_tokens": 5},
            },
        )
        out = ClaudeSessionRuntime().logs(_config())  # type: ignore[arg-type]
        assert "[user]" in out and "do X" in out
        assert "[assistant]" in out and "doing" in out
        assert "[result]" in out and "sess-1" in out and "out=5" in out

    def test_session_tail_respects_lines_arg(self, state_root: Path) -> None:
        sd = state_root / "alpha"
        for i in range(5):
            runner.append_session_message(
                sd, {"type": "assistant", "text": f"chunk-{i}"}
            )
        out = ClaudeSessionRuntime().logs(_config(), lines=2)  # type: ignore[arg-type]
        assert "chunk-3" in out and "chunk-4" in out
        assert "chunk-0" not in out


class TestPidAlive:
    def test_self_alive(self) -> None:
        assert _pid_alive(os.getpid()) is True

    def test_huge_pid_dead(self) -> None:
        assert _pid_alive(999_999_999) is False


# ---------------------------------------------------------------------------
# End-to-end: real subprocess via start/stop
# ---------------------------------------------------------------------------


class TestStartStopE2E:
    def test_start_spawns_runner_and_stop_kills_it(
        self, state_root: Path, monkeypatch
    ) -> None:
        # Override the runner default the spawned child will read so it
        # writes into our tmp_path. The adapter passes the agent name on
        # argv but does NOT pass --state-root, so the env var is the
        # only way to redirect.
        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(state_root))
        rt = ClaudeSessionRuntime()
        cfg = _config("e2e")
        assert rt.start(cfg) is True  # type: ignore[arg-type]
        try:
            assert rt.is_running(cfg) is True  # type: ignore[arg-type]
            # Wait briefly for the heartbeat to materialize.
            sd = state_root / "e2e"
            deadline = time.time() + 3.0
            while time.time() < deadline and not (sd / "heartbeat.json").exists():
                time.sleep(0.05)
            assert (sd / "heartbeat.json").is_file()
        finally:
            assert rt.stop(cfg) is True  # type: ignore[arg-type]
        assert rt.is_running(cfg) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# argv composition (no subprocess) — verify mission + resume forwarding
# ---------------------------------------------------------------------------


class TestArgvComposition:
    def test_no_mission_means_no_mission_arg(self, state_root: Path) -> None:
        from scitex_agent_container.runtimes import claude_session as adapter

        cfg = _config(startup_commands=[])
        captured: dict = {}

        class _FakePopen:
            def __init__(self, argv, **kw):
                captured["argv"] = argv
                self.pid = 999_999_999  # dead pid; start() will time out

            def poll(self):
                return 1

        with patch.object(adapter.subprocess, "Popen", _FakePopen):
            adapter.ClaudeSessionRuntime().start(cfg)  # type: ignore[arg-type]
        assert "--mission" not in captured["argv"]

    def test_mission_from_first_startup_command(self, state_root: Path) -> None:
        from scitex_agent_container.runtimes import claude_session as adapter

        cfg = _config(startup_commands=[_startup(""), _startup("Hello mission")])
        captured: dict = {}

        class _FakePopen:
            def __init__(self, argv, **kw):
                captured["argv"] = argv
                self.pid = 999_999_999

            def poll(self):
                return 1

        with patch.object(adapter.subprocess, "Popen", _FakePopen):
            adapter.ClaudeSessionRuntime().start(cfg)  # type: ignore[arg-type]
        assert (
            captured["argv"][captured["argv"].index("--mission") + 1] == "Hello mission"
        )

    def test_foreground_mode_inherits_stdio_and_blocks(self, state_root: Path) -> None:
        from scitex_agent_container.runtimes import claude_session as adapter

        cfg = _config(startup_commands=[_startup("hi")])
        captured: dict = {}

        class _FakePopen:
            def __init__(self, argv, **kw):
                captured["argv"] = argv
                captured["kwargs"] = kw
                self.pid = 1234

            def wait(self):
                return 0

            def send_signal(self, _s):
                pass

        with patch.object(adapter.subprocess, "Popen", _FakePopen):
            ok = adapter.ClaudeSessionRuntime().start(cfg, foreground=True)  # type: ignore[arg-type]
        assert ok is True
        # Foreground => no stdout/stderr/stdin redirection (those keys must
        # be absent so subprocess inherits the caller's tty).
        assert "stdout" not in captured["kwargs"]
        assert "stderr" not in captured["kwargs"]
        assert "stdin" not in captured["kwargs"]
        assert "start_new_session" not in captured["kwargs"]
        # Runner gets --print-stream so it mirrors assistant chunks.
        assert "--print-stream" in captured["argv"]

    def test_existing_session_id_triggers_resume(self, state_root: Path) -> None:
        from scitex_agent_container.runtimes import claude_session as adapter

        runner.write_session_id(state_root / "alpha", "prev-uuid")
        cfg = _config(startup_commands=[_startup("go")])
        captured: dict = {}

        class _FakePopen:
            def __init__(self, argv, **kw):
                captured["argv"] = argv
                self.pid = 999_999_999

            def poll(self):
                return 1

        with patch.object(adapter.subprocess, "Popen", _FakePopen):
            adapter.ClaudeSessionRuntime().start(cfg)  # type: ignore[arg-type]
        argv = captured["argv"]
        assert argv[argv.index("--resume-session-id") + 1] == "prev-uuid"

    def test_a2a_port_forwarded_when_yaml_declares_it(
        self, state_root: Path, tmp_path: Path
    ) -> None:
        """spec.a2a.port in YAML → runner argv gets --a2a-port + --a2a-host."""
        from scitex_agent_container.runtimes import claude_session as adapter

        yaml_path = tmp_path / "agent.yaml"
        yaml_path.write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "metadata:\n  name: alpha\n"
            "spec:\n"
            "  runtime: claude-session\n"
            "  a2a:\n    port: 18888\n    host: 0.0.0.0\n"
        )
        cfg = SimpleNamespace(
            name="alpha", startup_commands=[], config_path=str(yaml_path)
        )
        captured: dict = {}

        class _FakePopen:
            def __init__(self, argv, **kw):
                captured["argv"] = argv
                self.pid = 999_999_999

            def poll(self):
                return 1

        with patch.object(adapter.subprocess, "Popen", _FakePopen):
            adapter.ClaudeSessionRuntime().start(cfg)  # type: ignore[arg-type]
        argv = captured["argv"]
        assert argv[argv.index("--a2a-port") + 1] == "18888"
        assert argv[argv.index("--a2a-host") + 1] == "0.0.0.0"

    def test_remote_host_dispatches_via_ssh_in_foreground(
        self, state_root: Path
    ) -> None:
        """When spec.remote.host is set + --foreground, build an ssh+bash -l -s
        command and pipe the rendered launch script to its stdin."""
        from scitex_agent_container.runtimes import claude_session as adapter

        cfg_remote = SimpleNamespace(
            hops=[],
            host="some-remote-host",
            user="alice",
            key="",
            port=22,
            timeout=60,
            login_shell=True,
            no_preflight=False,
            is_remote=True,
        )
        cfg = SimpleNamespace(
            name="my-agent",
            startup_commands=[],
            remote=cfg_remote,
        )
        captured: dict = {}

        class _FakePopen:
            def __init__(self, argv, **kw):
                captured["argv"] = argv
                captured["kwargs"] = kw
                self.pid = 12345
                # Capture writes to stdin so the test can assert
                # the script content piped over.
                import io

                self.stdin = io.BytesIO()
                # Stash the buffer on captured so we can read it
                # after .close() — io.BytesIO.close() throws away,
                # so override close to no-op for the test.
                self.stdin.close = lambda: captured.update(
                    script=self.stdin.getvalue().decode("utf-8")
                )

            def wait(self):
                return 0

            def send_signal(self, _s):
                pass

        with patch.object(adapter.subprocess, "Popen", _FakePopen):
            ok = adapter.ClaudeSessionRuntime().start(cfg, foreground=True)  # type: ignore[arg-type]
        assert ok is True
        argv = captured["argv"]
        # Real ssh command, ending in bash -l -s
        assert argv[0] == "ssh"
        assert "alice@some-remote-host" in argv
        assert argv[-3:] == ["bash", "-l", "-s"]
        # Piped script sources the per-host hook and exec's the runner
        script = captured["script"]
        assert '[ -f "$_sac_hook" ] && . "$_sac_hook"' in script
        assert (
            "exec python3 -m scitex_agent_container._runners.claude_session" in script
        )
        assert "--name my-agent" in script
        assert "--print-stream" in script

    def test_no_a2a_port_when_yaml_omits_it(self, state_root: Path) -> None:
        """No spec.a2a block → no --a2a-port in argv."""
        from scitex_agent_container.runtimes import claude_session as adapter

        cfg = _config(startup_commands=[])
        captured: dict = {}

        class _FakePopen:
            def __init__(self, argv, **kw):
                captured["argv"] = argv
                self.pid = 999_999_999

            def poll(self):
                return 1

        with patch.object(adapter.subprocess, "Popen", _FakePopen):
            adapter.ClaudeSessionRuntime().start(cfg)  # type: ignore[arg-type]
        assert "--a2a-port" not in captured["argv"]
