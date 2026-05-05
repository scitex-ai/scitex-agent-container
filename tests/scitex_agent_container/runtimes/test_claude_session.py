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
            "  runtime: docker\n"
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
                import io

                self.stdin = io.BytesIO()
                self.stdin.close = lambda: captured.update(
                    script=self.stdin.getvalue().decode("utf-8")
                )

            def wait(self):
                return 0

            def send_signal(self, _s):
                pass

        # Patch _ssh_is_running so the start() pre-check returns False
        # without needing to mock subprocess.run shape; then patch Popen
        # to capture the foreground ssh-pipe call we actually care about.
        with (
            patch.object(adapter, "_ssh_is_running", return_value=False),
            patch.object(adapter.subprocess, "Popen", _FakePopen),
        ):
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
            "exec ${SAC_RUNNER_PREFIX:-} python3 -m scitex_agent_container._runners.claude_session"
            in script
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


# ---------------------------------------------------------------------------
# F-CS1: claude-session must materialise CLAUDE.md with hard/soft skills.
#
# Pins the contract the F-CS1 feature request specifies:
#   * spec.skills.required[]  -> HARD mode -> ``@<absolute path>`` lines in
#     CLAUDE.md so the SDK inlines the skill content at session start.
#   * spec.skills.available[] -> SOFT mode -> a reference listing
#     (``- <name>: <path>``) with NO ``@-import``; agent reads on demand.
# The runtime materialises CLAUDE.md via ``_setup_workspace`` before
# spawning the SDK runner subprocess, and tears it down via
# ``_cleanup_workspace`` on stop. Both helpers wrap the existing
# ``runtimes.claude_md`` primitives.
# ---------------------------------------------------------------------------

from scitex_agent_container.config import AgentConfig  # noqa: E402
from scitex_agent_container.config._types import (  # noqa: E402
    ClaudeSpec,
    SkillsSpec,
)


def _make_skill(root: Path, name: str, body: str = "skill body") -> Path:
    """Create ``<root>/<name>/SKILL.md`` with a frontmatter ``name:`` line.

    Mirrors the canonical Anthropic skill layout that ``_resolve_skill``
    consumes (``skill-id`` strategy walks ``<root>/.../<dir>/SKILL.md``
    and matches frontmatter ``name:`` ELSE the dir name).
    """
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = skill_dir / "SKILL.md"
    md.write_text(f"---\nname: {name}\n---\n\n{body}\n")
    return md


@pytest.fixture
def skill_roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build a temp skill-root layout with one HARD and one SOFT skill."""
    root = tmp_path / "skills"
    hard_md = _make_skill(root, "f-cs1-hard-skill", "load me eagerly")
    soft_md = _make_skill(root, "f-cs1-soft-skill", "read me on demand")
    return root, hard_md, soft_md


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """Per-agent workdir where ``.claude/CLAUDE.md`` will land."""
    wd = tmp_path / "agent-workdir"
    wd.mkdir()
    return wd


def _make_skills_config(workdir: Path, skill_root: Path) -> AgentConfig:
    """Real AgentConfig declaring both required + available skills.

    The ``--add-dir`` flag tells ``_resolve_skill`` where to look for
    skill markdown files (otherwise it falls back to ``~/.claude/skills``
    which the test suite must not depend on).
    """
    return AgentConfig(
        name="f-cs1-agent",
        runtime="claude-session",
        workdir=str(workdir),
        skills=SkillsSpec(
            required=["f-cs1-hard-skill"],
            available=["f-cs1-soft-skill"],
            injection_mode="at-import",
        ),
        claude=ClaudeSpec(flags=[f"--add-dir={skill_root}"]),
    )


class TestSetupWorkspace:
    """``_setup_workspace`` writes CLAUDE.md with hard + soft sections."""

    def test_required_skill_emits_at_import_line(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        """HARD mode: required skill resolves to ``@<absolute path>`` line."""
        skill_root, hard_md, _soft_md = skill_roots
        config = _make_skills_config(workdir, skill_root)

        ClaudeSessionRuntime()._setup_workspace(config)

        claude_md = workdir / ".claude" / "CLAUDE.md"
        assert claude_md.exists(), "CLAUDE.md must be created on setup"
        text = claude_md.read_text()

        # HARD: required skill emits an @-import line.
        assert f"@{hard_md}" in text, (
            "Required skill must materialise as '@<absolute path>' so the "
            "SDK inlines its content at session start (F-CS1 hard mode)."
        )

    def test_available_skill_emits_soft_listing_without_at_import(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        """SOFT mode: available skill listed by name+path; no ``@<path>``."""
        skill_root, _hard_md, soft_md = skill_roots
        config = _make_skills_config(workdir, skill_root)

        ClaudeSessionRuntime()._setup_workspace(config)

        claude_md = workdir / ".claude" / "CLAUDE.md"
        text = claude_md.read_text()

        # SOFT: available skill must NOT be eagerly @-imported.
        assert f"@{soft_md}" not in text, (
            "Available skill must be SOFT — no '@<path>' line (would defeat "
            "the lazy/reference-only contract in F-CS1)."
        )
        # SOFT: but it MUST appear as a reference listing (name + path)
        # so the agent knows the skill exists and where to read it.
        assert "### Available Skills" in text
        assert "f-cs1-soft-skill" in text
        assert str(soft_md) in text, (
            "Available skill's resolved path must still be visible in "
            "CLAUDE.md (just not @-imported)."
        )

    def test_managed_section_markers_present(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        """The agent-container section is delimited by stable HTML markers."""
        skill_root, _hard, _soft = skill_roots
        config = _make_skills_config(workdir, skill_root)

        ClaudeSessionRuntime()._setup_workspace(config)

        text = (workdir / ".claude" / "CLAUDE.md").read_text()
        assert '<!-- agent-container:start id="f-cs1-agent" -->' in text
        assert '<!-- agent-container:end id="f-cs1-agent" -->' in text

    def test_remote_config_skips_workspace_setup(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        """Remote agents materialise CLAUDE.md on the remote host, not here.

        ``_setup_workspace`` short-circuits when ``config.remote.is_remote``
        is True so we don't write CLAUDE.md into a workdir that maps to a
        path on a different machine.
        """
        skill_root, _hard, _soft = skill_roots
        config = _make_skills_config(workdir, skill_root)
        # Mark the config as remote.
        config.remote.host = "some-remote-box"
        # ``RemoteSpec.is_remote`` is a property derived from ``.host``, so
        # this is enough; no need to monkey-patch the dataclass.
        assert config.remote.is_remote is True

        ClaudeSessionRuntime()._setup_workspace(config)

        # No local CLAUDE.md should have been written for a remote agent.
        assert not (workdir / ".claude" / "CLAUDE.md").exists()


class TestCleanupWorkspace:
    """``_cleanup_workspace`` removes the managed section."""

    def test_cleanup_strips_managed_section(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        skill_root, _hard, _soft = skill_roots
        config = _make_skills_config(workdir, skill_root)

        runtime = ClaudeSessionRuntime()
        runtime._setup_workspace(config)
        claude_md = workdir / ".claude" / "CLAUDE.md"
        assert claude_md.exists()
        # Sanity: section was added.
        before = claude_md.read_text()
        assert "agent-container:start" in before

        runtime._cleanup_workspace(config)

        # File may still exist (cleanup_claude_md only strips the managed
        # block, preserving any user content) but our markers must be gone.
        if claude_md.exists():
            after = claude_md.read_text()
            assert "agent-container:start" not in after
            assert "f-cs1-hard-skill" not in after
            assert "f-cs1-soft-skill" not in after

    def test_cleanup_preserves_user_content(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        """User-authored content above/below our markers must survive cleanup."""
        skill_root, _hard, _soft = skill_roots
        # Pre-seed CLAUDE.md with user content.
        claude_dir = workdir / ".claude"
        claude_dir.mkdir(parents=True)
        claude_md = claude_dir / "CLAUDE.md"
        claude_md.write_text("# My Project\n\nuser stuff above\n")

        config = _make_skills_config(workdir, skill_root)
        runtime = ClaudeSessionRuntime()
        runtime._setup_workspace(config)
        # Verify user content + agent block coexist.
        text = claude_md.read_text()
        assert "# My Project" in text and "user stuff above" in text
        assert "agent-container:start" in text

        runtime._cleanup_workspace(config)

        after = claude_md.read_text()
        assert "# My Project" in after
        assert "user stuff above" in after
        assert "agent-container:start" not in after


# ---------------------------------------------------------------------------
# F-CS8 — heavy workdir/.claude/ precheck
# ---------------------------------------------------------------------------


class TestHeavyWorkdirClaudeWarning:
    """``_warn_if_heavy_workdir_claude`` surfaces the silent-SDK-failure
    risk before agent start (F-CS8).

    The SDK's ``<workdir>/.claude/`` auto-discovery silently swallows
    errors when the tree is large or contains failing hooks — the runner
    looks alive but every turn returns 0 tokens. We can't detect the
    SDK's behaviour in advance, but we CAN measure size and warn.
    """

    def test_no_warning_when_no_claude_dir(self, workdir, capsys):
        from scitex_agent_container.runtimes.claude_session import (
            _warn_if_heavy_workdir_claude,
        )

        config = AgentConfig(name="x", runtime="claude-session", workdir=str(workdir))
        _warn_if_heavy_workdir_claude(config)
        err = capsys.readouterr().err
        assert err == "", f"unexpected stderr: {err!r}"

    def test_no_warning_for_small_claude_dir(self, workdir, capsys):
        from scitex_agent_container.runtimes.claude_session import (
            _warn_if_heavy_workdir_claude,
        )

        (workdir / ".claude").mkdir()
        (workdir / ".claude" / "CLAUDE.md").write_text("x" * 1024)
        config = AgentConfig(name="x", runtime="claude-session", workdir=str(workdir))
        _warn_if_heavy_workdir_claude(config)
        err = capsys.readouterr().err
        assert err == "", f"small .claude must not warn, got: {err!r}"

    def test_warns_when_claude_dir_exceeds_threshold(
        self, workdir, capsys, monkeypatch
    ):
        """Lower the threshold so we don't have to write 10 MB to a tmp dir."""
        from scitex_agent_container.runtimes import claude_session as cs

        (workdir / ".claude" / "hooks").mkdir(parents=True)
        # 200 KB across two files
        (workdir / ".claude" / "hooks" / "big.sh").write_text("x" * 100 * 1024)
        (workdir / ".claude" / "skills.md").write_text("y" * 100 * 1024)

        # Threshold below the data we wrote.
        monkeypatch.setattr(cs, "_WORKDIR_CLAUDE_SIZE_WARN_BYTES", 50 * 1024)

        config = AgentConfig(name="x", runtime="claude-session", workdir=str(workdir))
        cs._warn_if_heavy_workdir_claude(config)

        err = capsys.readouterr().err
        assert "F-CS8" in err
        assert ".claude/" in err
        assert "0 tokens" in err
        assert str(workdir) in err

    def test_record_stop_best_effort_marks_ended_via_sidecar(
        self, tmp_path, monkeypatch
    ):
        """When a stop path runs and the state_dir holds an instance_id
        sidecar, ``_record_stop_best_effort`` must update the matching
        state.db row and clear the sidecar (F-CS11 phase 3)."""
        # Isolate state.db to a tmp_path the test owns.
        monkeypatch.setenv(
            "SCITEX_AGENT_CONTAINER_STATE_DB", str(tmp_path / "state.db")
        )
        import importlib

        import scitex_agent_container._state.state_db as state_db

        importlib.reload(state_db)

        from scitex_agent_container._runners._session_state import (
            read_instance_id,
            write_instance_id,
        )
        from scitex_agent_container.runtimes import claude_session as cs

        importlib.reload(cs)

        # Seed: insert a row, persist its id in a sidecar.
        iid = state_db.record_instance_start("test-stop", host="h")
        state_dir = tmp_path / "state-dir"
        state_dir.mkdir()
        write_instance_id(state_dir, iid)

        cs._record_stop_best_effort(state_dir, "stopped")

        with state_db.open_db() as conn:
            row = conn.execute(
                "SELECT ended_at, exit_reason FROM instances WHERE id=?", (iid,)
            ).fetchone()
        assert row["ended_at"] is not None
        assert row["exit_reason"] == "stopped"
        assert read_instance_id(state_dir) is None  # sidecar cleared

    def test_record_stop_best_effort_no_sidecar_is_noop(self, tmp_path, monkeypatch):
        """Stop on a state_dir without an instance_id sidecar is a quiet
        no-op (agents started before F-CS11 won't have one)."""
        monkeypatch.setenv(
            "SCITEX_AGENT_CONTAINER_STATE_DB", str(tmp_path / "state.db")
        )
        import importlib

        import scitex_agent_container.runtimes.claude_session as cs

        importlib.reload(cs)

        state_dir = tmp_path / "state-dir"
        state_dir.mkdir()
        # Should not raise, even though state.db has never been touched.
        cs._record_stop_best_effort(state_dir, "stopped")

    # ------------------------------------------------------------------
    # F-CS16 phase 2c — container dispatch wiring.
    # ------------------------------------------------------------------

    def test_container_runtime_for_returns_instance_for_docker_and_podman(
        self, tmp_path
    ):
        from scitex_agent_container.config import AgentConfig
        from scitex_agent_container.runtimes.claude_session import (
            _container_runtime_for,
        )

        for engine in ("docker", "podman"):
            cfg = AgentConfig(name="x", runtime=engine, workdir=str(tmp_path))
            rt = _container_runtime_for(cfg)
            assert rt is not None
            assert rt.engine == engine

    def test_container_runtime_for_returns_none_for_legacy(self, tmp_path):
        from scitex_agent_container.config import AgentConfig
        from scitex_agent_container.runtimes.claude_session import (
            _container_runtime_for,
        )

        for legacy in ("claude-code", "claude-session", "slurm", ""):
            cfg = AgentConfig(name="x", runtime=legacy, workdir=str(tmp_path))
            assert _container_runtime_for(cfg) is None, (
                f"legacy runtime {legacy!r} should not route to ContainerRuntime"
            )

    def test_container_runtime_for_returns_none_for_apptainer(self, tmp_path):
        """Apptainer runtime class lands in a follow-up; helper still
        returns None so callers fall through to bare-metal until then."""
        from scitex_agent_container.config import AgentConfig
        from scitex_agent_container.runtimes.claude_session import (
            _container_runtime_for,
        )

        cfg = AgentConfig(name="x", runtime="apptainer", workdir=str(tmp_path))
        assert _container_runtime_for(cfg) is None

    def test_start_dispatches_to_container_runtime_for_docker(
        self, tmp_path, monkeypatch
    ):
        from scitex_agent_container.config import AgentConfig
        from scitex_agent_container.runtimes import claude_session as cs

        seen = {}

        class _Stub:
            engine = "docker"

            def start(self, config, **kw):
                seen["start"] = (config.name, kw)
                return True

        monkeypatch.setattr(cs, "_container_runtime_for", lambda cfg: _Stub())
        monkeypatch.setattr(
            cs.ClaudeSessionRuntime, "_setup_workspace", lambda self, c: None
        )

        cfg = AgentConfig(name="capsule-01", runtime="docker", workdir=str(tmp_path))
        assert cs.ClaudeSessionRuntime().start(cfg, dry_run=True) is True
        assert seen["start"][0] == "capsule-01"
        assert seen["start"][1]["dry_run"] is True

    def test_stop_dispatches_to_container_runtime_for_docker(
        self, tmp_path, monkeypatch
    ):
        from scitex_agent_container.config import AgentConfig
        from scitex_agent_container.runtimes import claude_session as cs

        seen = {}

        class _Stub:
            engine = "docker"

            def stop(self, config):
                seen["stop"] = config.name
                return True

        monkeypatch.setattr(cs, "_container_runtime_for", lambda cfg: _Stub())
        monkeypatch.setattr(
            cs.ClaudeSessionRuntime, "_cleanup_workspace", lambda self, c: None
        )

        cfg = AgentConfig(name="x", runtime="docker", workdir=str(tmp_path))
        assert cs.ClaudeSessionRuntime().stop(cfg) is True
        assert seen["stop"] == "x"

    def test_is_running_dispatches_to_container_runtime_for_docker(
        self, tmp_path, monkeypatch
    ):
        from scitex_agent_container.config import AgentConfig
        from scitex_agent_container.runtimes import claude_session as cs

        class _Stub:
            engine = "docker"

            def is_running(self, config):
                return True

        monkeypatch.setattr(cs, "_container_runtime_for", lambda cfg: _Stub())
        cfg = AgentConfig(name="x", runtime="docker", workdir=str(tmp_path))
        assert cs.ClaudeSessionRuntime().is_running(cfg) is True

    def test_logs_falls_through_to_container_logs_when_no_session_yet(
        self, tmp_path, monkeypatch
    ):
        """When there's no session.jsonl yet, container-mode logs come
        from ``docker logs --tail N`` instead of the heartbeat dump."""
        from scitex_agent_container.config import AgentConfig
        from scitex_agent_container.runtimes import claude_session as cs

        class _Stub:
            engine = "docker"

            def logs(self, config, lines=50):
                return f"DOCKER LOGS lines={lines}"

        monkeypatch.setattr(cs, "_container_runtime_for", lambda cfg: _Stub())
        cfg = AgentConfig(name="x", runtime="docker", workdir=str(tmp_path))
        assert cs.ClaudeSessionRuntime().logs(cfg, lines=12) == "DOCKER LOGS lines=12"

    def test_symlinks_are_not_followed(self, workdir, tmp_path, capsys, monkeypatch):
        """Symlinked targets must NOT inflate the size estimate."""
        from scitex_agent_container.runtimes import claude_session as cs

        (workdir / ".claude").mkdir()
        # 200 KB outside of .claude/, exposed as a symlink inside.
        big = tmp_path / "external-big.txt"
        big.write_text("z" * 200 * 1024)
        (workdir / ".claude" / "linked").symlink_to(big)

        monkeypatch.setattr(cs, "_WORKDIR_CLAUDE_SIZE_WARN_BYTES", 50 * 1024)

        config = AgentConfig(name="x", runtime="claude-session", workdir=str(workdir))
        cs._warn_if_heavy_workdir_claude(config)
        err = capsys.readouterr().err
        assert err == "", (
            "symlinked content must not count toward the size threshold; "
            f"got warning: {err!r}"
        )
