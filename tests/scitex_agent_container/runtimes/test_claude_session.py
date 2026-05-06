"""Tests for ``runtimes/claude_session.py`` (F-CS17 minimal version).

After F-CS17 stage 3a deleted the bare-metal subprocess and
SSH-dispatch paths, ClaudeSessionRuntime is a thin shim that:

  * Materialises CLAUDE.md before the container starts (F-CS1).
  * Surfaces the F-CS8 heavy-workdir warning.
  * Delegates start / stop / is_running / logs to ContainerRuntime.

The bare-metal tests (TestIsRunning, TestStop, TestPidAlive, the
real-subprocess end-to-end, the argv-composition / SSH-dispatch
tests) were deleted alongside the code they exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ClaudeSpec, SkillsSpec
from scitex_agent_container.runtimes.claude_session import (
    ClaudeSessionRuntime,
    _container_runtime_for,
    _format_session_tail,
    _warn_if_heavy_workdir_claude,
    _workdir_claude_size_bytes,
)

# ---------------------------------------------------------------------------
# F-CS16 phase 2c — container-engine dispatch helper.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ["docker", "podman"])
def test_container_runtime_for_returns_instance(tmp_path, engine):
    cfg = AgentConfig(name="x", runtime=engine, workdir=str(tmp_path))
    rt = _container_runtime_for(cfg)
    assert rt is not None
    assert rt.engine == engine


def test_container_runtime_for_returns_apptainer_instance(tmp_path):
    """F-CS18 — `runtime: apptainer` returns ApptainerContainerRuntime."""
    cfg = AgentConfig(name="x", runtime="apptainer", workdir=str(tmp_path))
    rt = _container_runtime_for(cfg)
    assert rt is not None
    assert rt.engine == "apptainer"
    assert type(rt).__name__ == "ApptainerContainerRuntime"


@pytest.mark.parametrize("runtime", ["", "claude-session", "claude-code", "slurm"])
def test_container_runtime_for_returns_none_for_unknown(tmp_path, runtime):
    """Legacy runtimes are rejected by the validator anyway; the helper
    returns None for runtimes it doesn't know how to dispatch."""
    cfg = AgentConfig(name="x", runtime=runtime, workdir=str(tmp_path))
    assert _container_runtime_for(cfg) is None


# ---------------------------------------------------------------------------
# Lifecycle delegation — start / stop / is_running / logs all hit
# ContainerRuntime when the runtime is docker / podman.
# ---------------------------------------------------------------------------


class _FakeContainerRuntime:
    engine = "docker"

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._is_running = False
        self._logs = ""

    def start(self, config, **kw):
        self.calls.append(("start", {"name": config.name, **kw}))
        self._is_running = True
        return True

    def stop(self, config):
        self.calls.append(("stop", {"name": config.name}))
        self._is_running = False
        return True

    def is_running(self, config):
        self.calls.append(("is_running", {"name": config.name}))
        return self._is_running

    def logs(self, config, lines=50):
        self.calls.append(("logs", {"name": config.name, "lines": lines}))
        return self._logs


@pytest.fixture
def stub_container_rt(monkeypatch):
    fake = _FakeContainerRuntime()
    from scitex_agent_container.runtimes import claude_session as cs

    monkeypatch.setattr(cs, "_container_runtime_for", lambda cfg: fake)
    # Avoid touching the real CLAUDE.md materialiser — these tests
    # only care about the dispatch glue.
    monkeypatch.setattr(
        cs.ClaudeSessionRuntime, "_setup_workspace", lambda self, c: None
    )
    monkeypatch.setattr(
        cs.ClaudeSessionRuntime, "_cleanup_workspace", lambda self, c: None
    )
    return fake


def test_start_delegates_to_container_runtime(stub_container_rt, tmp_path):
    cfg = AgentConfig(name="capsule-01", runtime="docker", workdir=str(tmp_path))
    assert ClaudeSessionRuntime().start(cfg, dry_run=True, force=True) is True
    method, kw = stub_container_rt.calls[0]
    assert method == "start"
    assert kw["name"] == "capsule-01"
    assert kw["dry_run"] is True
    assert kw["force"] is True


def test_start_returns_false_when_no_container_engine(tmp_path, capsys):
    """Bare-metal is gone; legacy runtimes can't start anywhere."""
    cfg = AgentConfig(name="x", runtime="claude-session", workdir=str(tmp_path))
    assert ClaudeSessionRuntime().start(cfg) is False
    err = capsys.readouterr().err
    assert "container engine" in err.lower()


def test_stop_delegates_and_runs_cleanup(stub_container_rt, tmp_path):
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(tmp_path))
    assert ClaudeSessionRuntime().stop(cfg) is True
    methods = [m for m, _ in stub_container_rt.calls]
    assert "stop" in methods


def test_stop_returns_false_when_no_container_engine(tmp_path):
    cfg = AgentConfig(name="x", runtime="claude-session", workdir=str(tmp_path))
    assert ClaudeSessionRuntime().stop(cfg) is False


def test_is_running_delegates(stub_container_rt, tmp_path):
    stub_container_rt._is_running = True
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(tmp_path))
    assert ClaudeSessionRuntime().is_running(cfg) is True


def test_logs_returns_session_tail_when_present(
    stub_container_rt, tmp_path, monkeypatch
):
    """When session.jsonl exists, logs() prefers the rendered tail."""
    from scitex_agent_container.runtimes import claude_session as cs

    monkeypatch.setattr(cs.ClaudeSessionRuntime, "_state_dir", lambda self, c: tmp_path)
    (tmp_path / "session.jsonl").write_text('{"type":"user","text":"hello"}\n')
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(tmp_path))
    out = ClaudeSessionRuntime().logs(cfg)
    assert "[user]" in out
    assert "hello" in out


def test_logs_falls_through_to_container_when_no_session_yet(
    stub_container_rt, tmp_path, monkeypatch
):
    from scitex_agent_container.runtimes import claude_session as cs

    monkeypatch.setattr(cs.ClaudeSessionRuntime, "_state_dir", lambda self, c: tmp_path)
    stub_container_rt._logs = "DOCKER LOGS"
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(tmp_path))
    assert ClaudeSessionRuntime().logs(cfg) == "DOCKER LOGS"


# ---------------------------------------------------------------------------
# F-CS8 — heavy workdir/.claude/ warning (kept verbatim from before).
# ---------------------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "agent-workdir"
    wd.mkdir()
    return wd


def test_no_warning_when_no_claude_dir(workdir, capsys):
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(workdir))
    _warn_if_heavy_workdir_claude(cfg)
    assert capsys.readouterr().err == ""


def test_no_warning_for_small_claude_dir(workdir, capsys):
    (workdir / ".claude").mkdir()
    (workdir / ".claude" / "CLAUDE.md").write_text("x" * 1024)
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(workdir))
    _warn_if_heavy_workdir_claude(cfg)
    assert capsys.readouterr().err == ""


def test_warns_when_claude_dir_exceeds_threshold(workdir, capsys, monkeypatch):
    from scitex_agent_container.runtimes import claude_session as cs

    (workdir / ".claude" / "hooks").mkdir(parents=True)
    (workdir / ".claude" / "hooks" / "big.sh").write_text("x" * 100 * 1024)
    (workdir / ".claude" / "skills.md").write_text("y" * 100 * 1024)
    monkeypatch.setattr(cs, "_WORKDIR_CLAUDE_SIZE_WARN_BYTES", 50 * 1024)

    cfg = AgentConfig(name="x", runtime="docker", workdir=str(workdir))
    cs._warn_if_heavy_workdir_claude(cfg)
    err = capsys.readouterr().err
    assert "F-CS8" in err
    assert "0 tokens" in err


def test_workdir_claude_size_does_not_follow_symlinks(workdir, tmp_path, monkeypatch):
    """Symlinked content must NOT inflate the size estimate."""
    (workdir / ".claude").mkdir()
    big = tmp_path / "external-big.txt"
    big.write_text("z" * 200 * 1024)
    (workdir / ".claude" / "linked").symlink_to(big)
    assert _workdir_claude_size_bytes(str(workdir)) == 0


# ---------------------------------------------------------------------------
# F-CS1 — CLAUDE.md materialisation through _setup_workspace.
# ---------------------------------------------------------------------------


def _make_skill(root: Path, name: str, body: str = "skill body") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = skill_dir / "SKILL.md"
    md.write_text(f"---\nname: {name}\n---\n\n{body}\n")
    return md


@pytest.fixture
def skill_roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "skills"
    hard_md = _make_skill(root, "f-cs1-hard-skill", "load me eagerly")
    soft_md = _make_skill(root, "f-cs1-soft-skill", "read me on demand")
    return root, hard_md, soft_md


def _make_skills_config(workdir: Path, skill_root: Path) -> AgentConfig:
    return AgentConfig(
        name="f-cs1-agent",
        runtime="docker",
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
        skill_root, hard_md, _soft_md = skill_roots
        config = _make_skills_config(workdir, skill_root)

        ClaudeSessionRuntime()._setup_workspace(config)

        claude_md = workdir / ".claude" / "CLAUDE.md"
        assert claude_md.exists()
        assert f"@{hard_md}" in claude_md.read_text()

    def test_available_skill_emits_soft_listing_without_at_import(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        skill_root, _hard_md, soft_md = skill_roots
        config = _make_skills_config(workdir, skill_root)

        ClaudeSessionRuntime()._setup_workspace(config)

        text = (workdir / ".claude" / "CLAUDE.md").read_text()
        assert f"@{soft_md}" not in text
        assert "f-cs1-soft-skill" in text

    def test_managed_section_markers_present(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        skill_root, _hard, _soft = skill_roots
        config = _make_skills_config(workdir, skill_root)

        ClaudeSessionRuntime()._setup_workspace(config)
        text = (workdir / ".claude" / "CLAUDE.md").read_text()
        assert "agent-container:start" in text
        assert "agent-container:end" in text


class TestCleanupWorkspace:
    def test_cleanup_strips_managed_section(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        skill_root, _hard, _soft = skill_roots
        config = _make_skills_config(workdir, skill_root)
        runtime = ClaudeSessionRuntime()
        runtime._setup_workspace(config)
        runtime._cleanup_workspace(config)
        claude_md = workdir / ".claude" / "CLAUDE.md"
        if claude_md.exists():
            assert "agent-container:start" not in claude_md.read_text()

    def test_cleanup_preserves_user_content(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        skill_root, _hard, _soft = skill_roots
        # Pre-populate CLAUDE.md with user content.
        (workdir / ".claude").mkdir()
        claude_md = workdir / ".claude" / "CLAUDE.md"
        claude_md.write_text("# My Project\n\nuser stuff above\n")

        config = _make_skills_config(workdir, skill_root)
        runtime = ClaudeSessionRuntime()
        runtime._setup_workspace(config)
        runtime._cleanup_workspace(config)

        after = claude_md.read_text()
        assert "# My Project" in after
        assert "user stuff above" in after
        assert "agent-container:start" not in after


# ---------------------------------------------------------------------------
# session.jsonl renderer (host-side, used by logs()).
# ---------------------------------------------------------------------------


def test_format_session_tail_renders_user_assistant_result(tmp_path):
    sess = tmp_path / "session.jsonl"
    sess.write_text(
        '{"type":"user","text":"do thing"}\n'
        '{"type":"assistant","text":"thinking"}\n'
        '{"type":"result","session_id":"abc","usage":{"input_tokens":10,"output_tokens":5}}\n'
    )
    out = _format_session_tail(tmp_path, max_lines=10)
    assert "[user]" in out and "do thing" in out
    assert "[assistant]" in out and "thinking" in out
    assert "[result]" in out and "session=abc" in out
    assert "in=10" in out and "out=5" in out


def test_format_session_tail_returns_empty_when_missing(tmp_path):
    assert _format_session_tail(tmp_path, max_lines=10) == ""


def test_format_session_tail_respects_max_lines(tmp_path):
    sess = tmp_path / "session.jsonl"
    sess.write_text(
        "\n".join(f'{{"type":"assistant","text":"chunk-{i}"}}' for i in range(20))
        + "\n"
    )
    out = _format_session_tail(tmp_path, max_lines=3)
    assert out.count("[assistant]") == 3
    # Last 3 chunks should be 17, 18, 19 — not 0, 1, 2.
    assert "chunk-19" in out
    assert "chunk-0" not in out
