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
    _materialize_home_layouts,
    _warn_if_heavy_workdir_claude,
    _workdir_claude_size_bytes,
)

# ---------------------------------------------------------------------------
# F-CS16 phase 2c — container-engine dispatch helper.
# ---------------------------------------------------------------------------


def test_container_runtime_for_apptainer_returns_non_none(tmp_path):
    """Sac is apptainer-only since 2026-05-13."""
    # Arrange
    cfg = AgentConfig(name="x", runtime="apptainer", workdir=str(tmp_path))
    # Act
    rt = _container_runtime_for(cfg)
    # Assert
    assert rt is not None


def test_container_runtime_for_apptainer_sets_engine(tmp_path):
    # Arrange
    cfg = AgentConfig(name="x", runtime="apptainer", workdir=str(tmp_path))
    # Act
    rt = _container_runtime_for(cfg)
    # Assert
    assert rt.engine == "apptainer"


def test_container_runtime_for_apptainer_returns_apptainer_class(tmp_path):
    # Arrange
    cfg = AgentConfig(name="x", runtime="apptainer", workdir=str(tmp_path))
    # Act
    rt = _container_runtime_for(cfg)
    # Assert
    assert type(rt).__name__ == "ApptainerContainerRuntime"


def test_container_runtime_for_empty_returns_non_none(tmp_path):
    """Unset `spec.runtime` defaults to apptainer at dispatch."""
    # Arrange
    cfg = AgentConfig(name="x", runtime="", workdir=str(tmp_path))
    # Act
    rt = _container_runtime_for(cfg)
    # Assert
    assert rt is not None


def test_container_runtime_for_empty_returns_apptainer_class(tmp_path):
    # Arrange
    cfg = AgentConfig(name="x", runtime="", workdir=str(tmp_path))
    # Act
    rt = _container_runtime_for(cfg)
    # Assert
    assert type(rt).__name__ == "ApptainerContainerRuntime"


@pytest.mark.parametrize("runtime", ["claude-session", "claude-code", "slurm"])
def test_container_runtime_for_returns_none_for_unknown(tmp_path, runtime):
    """Legacy runtimes are rejected by the validator anyway; the helper
    returns None for runtimes it doesn't know how to dispatch."""
    # Arrange
    cfg = AgentConfig(name="x", runtime=runtime, workdir=str(tmp_path))
    # Act
    rt = _container_runtime_for(cfg)
    # Assert
    assert rt is None


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


class _TestableRuntime(ClaudeSessionRuntime):
    """Subclass for tests — overrides workspace + state_dir.

    Replaces monkeypatch.setattr on _setup_workspace / _cleanup_workspace
    / _state_dir with a proper subclass override. The container runtime
    is injected through the public ``container_runtime_for=`` ctor arg.
    """

    def __init__(self, fake_rt, state_dir):
        super().__init__(container_runtime_for=lambda cfg: fake_rt)
        self._fixed_state_dir = state_dir

    def _setup_workspace(self, config):  # noqa: ARG002
        pass

    def _cleanup_workspace(self, config):  # noqa: ARG002
        pass

    def _state_dir(self, config):  # noqa: ARG002
        return self._fixed_state_dir


@pytest.fixture
def stub_container_rt():
    return _FakeContainerRuntime()


def test_start_delegates_to_container_runtime(stub_container_rt, tmp_path):
    # Arrange
    cfg = AgentConfig(name="capsule-01", runtime="docker", workdir=str(tmp_path))
    rt = _TestableRuntime(stub_container_rt, tmp_path)
    # Act
    result = rt.start(cfg, dry_run=True, force=True)
    # Assert
    assert result is True


def test_start_forwards_dry_run_kwarg_to_container_runtime(stub_container_rt, tmp_path):
    # Arrange
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(tmp_path))
    rt = _TestableRuntime(stub_container_rt, tmp_path)
    # Act
    rt.start(cfg, dry_run=True, force=True)
    # Assert
    _method, kw = stub_container_rt.calls[0]
    assert kw["dry_run"] is True


def test_start_accepts_one_shot_kwarg_without_raising(stub_container_rt, tmp_path):
    # Arrange — reproducer for the cohort-A blocker:
    # ``sac agents start --one-shot`` threads
    # ``start_kw["one_shot"] = True`` through
    # ``runtime.start(config, **start_kw)`` in
    # ``_lifecycle/_start.py:440``. Without this kwarg on the
    # runtime signature the CLI crashes with
    # ``TypeError: ClaudeSessionRuntime.start() got an unexpected
    # keyword argument 'one_shot'`` and the launcher cannot drive a
    # nested-sac dispatch.
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(tmp_path))
    rt = _TestableRuntime(stub_container_rt, tmp_path)
    # Act
    result = rt.start(cfg, one_shot=True)
    # Assert — return value is the fake container rt's True; the
    # test is that the kwarg DID NOT raise TypeError on the runtime
    # signature.
    assert result is True


def test_start_forwards_one_shot_kwarg_to_container_runtime(
    stub_container_rt, tmp_path
):
    # Arrange — ``one_shot`` is a no-op at this runtime layer (the
    # one-shot termination semantics come from
    # ``spec.autonomous.drive_until="DONE"``), but the kwarg is
    # forwarded to the underlying container runtime so any future
    # support there works without another signature change. Pin
    # the forwarding so a later refactor cannot silently drop it.
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(tmp_path))
    rt = _TestableRuntime(stub_container_rt, tmp_path)
    # Act
    rt.start(cfg, one_shot=True)
    # Assert
    _method, kw = stub_container_rt.calls[0]
    assert kw["one_shot"] is True


def test_start_returns_false_when_no_container_engine(tmp_path, capsys):
    # Arrange — legacy runtime that _container_runtime_for returns None for
    cfg = AgentConfig(name="x", runtime="claude-session", workdir=str(tmp_path))
    rt = ClaudeSessionRuntime()  # real lookup → None
    # Act
    result = rt.start(cfg)
    # Assert
    assert result is False


def test_stop_delegates_to_container_runtime(stub_container_rt, tmp_path):
    # Arrange
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(tmp_path))
    rt = _TestableRuntime(stub_container_rt, tmp_path)
    # Act
    result = rt.stop(cfg)
    # Assert
    assert result is True


def test_stop_returns_false_when_no_container_engine(tmp_path):
    # Arrange
    cfg = AgentConfig(name="x", runtime="claude-session", workdir=str(tmp_path))
    rt = ClaudeSessionRuntime()  # real lookup → None
    # Act
    result = rt.stop(cfg)
    # Assert
    assert result is False


def test_is_running_delegates_to_container_runtime(stub_container_rt, tmp_path):
    # Arrange
    stub_container_rt._is_running = True
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(tmp_path))
    rt = _TestableRuntime(stub_container_rt, tmp_path)
    # Act
    result = rt.is_running(cfg)
    # Assert
    assert result is True


def test_logs_returns_session_tail_when_present(stub_container_rt, tmp_path):
    # Arrange
    (tmp_path / "session.jsonl").write_text('{"type":"user","text":"hello"}\n')
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(tmp_path))
    rt = _TestableRuntime(stub_container_rt, tmp_path)
    # Act
    out = rt.logs(cfg)
    # Assert
    assert "hello" in out


def test_logs_falls_through_to_container_when_no_session_yet(
    stub_container_rt, tmp_path
):
    # Arrange
    stub_container_rt._logs = "DOCKER LOGS"
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(tmp_path))
    rt = _TestableRuntime(stub_container_rt, tmp_path)
    # Act
    out = rt.logs(cfg)
    # Assert
    assert out == "DOCKER LOGS"


# ---------------------------------------------------------------------------
# F-CS8 — heavy workdir/.claude/ warning (kept verbatim from before).
# ---------------------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "agent-workdir"
    wd.mkdir()
    return wd


def test_no_warning_when_no_claude_dir(workdir, capsys):
    # Arrange
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(workdir))
    # Act
    _warn_if_heavy_workdir_claude(cfg)
    # Assert
    assert capsys.readouterr().err == ""


def test_no_warning_for_small_claude_dir(workdir, capsys):
    # Arrange
    (workdir / ".claude").mkdir()
    (workdir / ".claude" / "CLAUDE.md").write_text("x" * 1024)
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(workdir))
    # Act
    _warn_if_heavy_workdir_claude(cfg)
    # Assert — the F-CS8 heavy-workdir banner must NOT fire for a tiny
    # tree. The audit chain's gdu/du missing-binary fallback warnings are
    # environment noise (CI runners without dundee gdu installed) and are
    # NOT what this test is asserting against.
    assert "F-CS8" not in capsys.readouterr().err


def test_warns_when_claude_dir_exceeds_threshold(workdir, capsys, env_save_restore):
    # Arrange — lower threshold via env override + write large .claude tree
    from scitex_agent_container.runtimes import claude_session as cs

    env_save_restore.set("SAC_WORKDIR_CLAUDE_WARN_BYTES", str(50 * 1024))
    (workdir / ".claude" / "hooks").mkdir(parents=True)
    (workdir / ".claude" / "hooks" / "big.sh").write_text("x" * 100 * 1024)
    (workdir / ".claude" / "skills.md").write_text("y" * 100 * 1024)
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(workdir))
    # Act
    cs._warn_if_heavy_workdir_claude(cfg)
    # Assert
    err = capsys.readouterr().err
    assert "F-CS8" in err


def test_workdir_claude_size_does_not_follow_symlinks(workdir, tmp_path):
    """Symlinked content must NOT inflate the size estimate."""
    # Arrange
    (workdir / ".claude").mkdir()
    big = tmp_path / "external-big.txt"
    big.write_text("z" * 200 * 1024)
    (workdir / ".claude" / "linked").symlink_to(big)
    # Act
    size = _workdir_claude_size_bytes(str(workdir))
    # Assert
    assert size == 0


# ---------------------------------------------------------------------------
# F-CS8 LOUD-warn upgrades (2026-06-03 hardening) — file-count threshold
# crossing trips the warn even with sub-threshold bytes, and the warn
# banner names the bloat-source subdirs by relative path so the operator
# can `mv` them without spelunking.
# ---------------------------------------------------------------------------


def test_warns_on_file_count_alone(workdir, capsys, env_save_restore):
    # Arrange — tiny bytes (10 × 1 B) but force file-count threshold low
    # enough that the count alone trips the F-CS8 warn. Confirms the warn
    # now uses BOTH thresholds (the orochi failure was count-driven).
    from scitex_agent_container.runtimes import claude_session as cs

    env_save_restore.set("SAC_WORKDIR_CLAUDE_WARN_FILES", "5")
    (workdir / ".claude" / "junk").mkdir(parents=True)
    for i in range(10):
        (workdir / ".claude" / "junk" / f"f{i}").write_bytes(b"x")
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(workdir))
    # Act
    cs._warn_if_heavy_workdir_claude(cfg)
    # Assert
    err = capsys.readouterr().err
    assert "F-CS8" in err


def test_warn_banner_lists_bloat_source_subdir(workdir, capsys, env_save_restore):
    # Arrange — drop the per-subdir bloat threshold so a small fixture
    # trips the worktrees bucket. The LOUD warn should name the subdir
    # in the banner so operators don't have to grep the whole tree.
    from scitex_agent_container.runtimes import claude_session as cs

    env_save_restore.set("SAC_WORKDIR_CLAUDE_WARN_FILES", "3")
    env_save_restore.set("SAC_WORKDIR_CLAUDE_BLOAT_SUBDIR_FILES", "3")
    (workdir / ".claude" / "worktrees").mkdir(parents=True)
    for i in range(5):
        (workdir / ".claude" / "worktrees" / f"f{i}").write_bytes(b"x")
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(workdir))
    # Act
    cs._warn_if_heavy_workdir_claude(cfg)
    # Assert
    err = capsys.readouterr().err
    assert ".claude/worktrees/" in err


def test_warn_banner_emits_concrete_mv_command(workdir, capsys, env_save_restore):
    # Arrange — operator-friendly: the banner should hand the operator a
    # ready-to-paste `mv` line for each bloat source.
    from scitex_agent_container.runtimes import claude_session as cs

    env_save_restore.set("SAC_WORKDIR_CLAUDE_WARN_FILES", "3")
    env_save_restore.set("SAC_WORKDIR_CLAUDE_BLOAT_SUBDIR_FILES", "3")
    (workdir / ".claude" / "worktrees").mkdir(parents=True)
    for i in range(5):
        (workdir / ".claude" / "worktrees" / f"f{i}").write_bytes(b"x")
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(workdir))
    # Act
    cs._warn_if_heavy_workdir_claude(cfg)
    # Assert
    err = capsys.readouterr().err
    assert "mv " in err


def test_warn_silent_when_neither_threshold_crossed(workdir, capsys, env_save_restore):
    # Arrange — generous thresholds, small tree. No F-CS8 banner expected.
    from scitex_agent_container.runtimes import claude_session as cs

    env_save_restore.set("SAC_WORKDIR_CLAUDE_WARN_FILES", "100000")
    env_save_restore.set("SAC_WORKDIR_CLAUDE_WARN_BYTES", str(1024 * 1024 * 1024))
    (workdir / ".claude" / "skills").mkdir(parents=True)
    (workdir / ".claude" / "skills" / "tiny.md").write_text("x")
    cfg = AgentConfig(name="x", runtime="docker", workdir=str(workdir))
    # Act
    cs._warn_if_heavy_workdir_claude(cfg)
    # Assert — the gdu/du missing-binary chain may emit environment
    # warnings on CI; this test asserts only that the F-CS8 heavy-workdir
    # banner does NOT fire when both thresholds are generously above
    # actual usage.
    assert "F-CS8" not in capsys.readouterr().err


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


def _claude_md_path(runtime: ClaudeSessionRuntime, config: AgentConfig) -> Path:
    """ADR-0003: CLAUDE.md materialises into ``runtime/<name>/home/.claude/``,
    not ``workdir/.claude/``."""
    return runtime._state_dir(config) / "home" / ".claude" / "CLAUDE.md"


class TestSetupWorkspace:
    """``_setup_workspace`` writes CLAUDE.md with hard + soft sections."""

    def test_required_skill_writes_claude_md(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        # Arrange
        skill_root, _hard_md, _soft_md = skill_roots
        config = _make_skills_config(workdir, skill_root)
        runtime = ClaudeSessionRuntime()
        # Act
        runtime._setup_workspace(config)
        # Assert
        assert _claude_md_path(runtime, config).exists()

    def test_required_skill_emits_at_import_line(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        # Arrange
        skill_root, hard_md, _soft_md = skill_roots
        config = _make_skills_config(workdir, skill_root)
        runtime = ClaudeSessionRuntime()
        # Act
        runtime._setup_workspace(config)
        # Assert
        assert f"@{hard_md}" in _claude_md_path(runtime, config).read_text()

    def test_available_skill_omits_at_import_for_soft(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        # Arrange
        skill_root, _hard_md, soft_md = skill_roots
        config = _make_skills_config(workdir, skill_root)
        runtime = ClaudeSessionRuntime()
        # Act
        runtime._setup_workspace(config)
        # Assert
        assert f"@{soft_md}" not in _claude_md_path(runtime, config).read_text()

    def test_available_skill_listed_by_name(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        # Arrange
        skill_root, _hard_md, _soft_md = skill_roots
        config = _make_skills_config(workdir, skill_root)
        runtime = ClaudeSessionRuntime()
        # Act
        runtime._setup_workspace(config)
        # Assert
        assert "f-cs1-soft-skill" in _claude_md_path(runtime, config).read_text()

    def test_managed_section_start_marker_present(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        # Arrange
        skill_root, _hard, _soft = skill_roots
        config = _make_skills_config(workdir, skill_root)
        runtime = ClaudeSessionRuntime()
        # Act
        runtime._setup_workspace(config)
        # Assert
        assert "agent-container:start" in _claude_md_path(runtime, config).read_text()

    def test_managed_section_end_marker_present(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        # Arrange
        skill_root, _hard, _soft = skill_roots
        config = _make_skills_config(workdir, skill_root)
        runtime = ClaudeSessionRuntime()
        # Act
        runtime._setup_workspace(config)
        # Assert
        assert "agent-container:end" in _claude_md_path(runtime, config).read_text()


class TestCleanupWorkspace:
    def test_cleanup_strips_managed_section(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        # Arrange
        skill_root, _hard, _soft = skill_roots
        config = _make_skills_config(workdir, skill_root)
        runtime = ClaudeSessionRuntime()
        runtime._setup_workspace(config)
        # Act
        runtime._cleanup_workspace(config)
        # Assert
        claude_md = _claude_md_path(runtime, config)
        if claude_md.exists():
            assert "agent-container:start" not in claude_md.read_text()

    def test_cleanup_preserves_user_heading(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        # Arrange
        skill_root, _hard, _soft = skill_roots
        (workdir / ".claude").mkdir()
        claude_md = workdir / ".claude" / "CLAUDE.md"
        claude_md.write_text("# My Project\n\nuser stuff above\n")
        config = _make_skills_config(workdir, skill_root)
        runtime = ClaudeSessionRuntime()
        runtime._setup_workspace(config)
        # Act
        runtime._cleanup_workspace(config)
        # Assert
        assert "# My Project" in claude_md.read_text()

    def test_cleanup_preserves_user_body(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        # Arrange
        skill_root, _hard, _soft = skill_roots
        (workdir / ".claude").mkdir()
        claude_md = workdir / ".claude" / "CLAUDE.md"
        claude_md.write_text("# My Project\n\nuser stuff above\n")
        config = _make_skills_config(workdir, skill_root)
        runtime = ClaudeSessionRuntime()
        runtime._setup_workspace(config)
        # Act
        runtime._cleanup_workspace(config)
        # Assert
        assert "user stuff above" in claude_md.read_text()

    def test_cleanup_removes_managed_marker_when_user_content_present(
        self, workdir: Path, skill_roots: tuple[Path, Path, Path]
    ) -> None:
        # Arrange
        skill_root, _hard, _soft = skill_roots
        (workdir / ".claude").mkdir()
        claude_md = workdir / ".claude" / "CLAUDE.md"
        claude_md.write_text("# My Project\n\nuser stuff above\n")
        config = _make_skills_config(workdir, skill_root)
        runtime = ClaudeSessionRuntime()
        runtime._setup_workspace(config)
        # Act
        runtime._cleanup_workspace(config)
        # Assert
        assert "agent-container:start" not in claude_md.read_text()


# ---------------------------------------------------------------------------
# session.jsonl renderer (host-side, used by logs()).
# ---------------------------------------------------------------------------


@pytest.fixture
def _session_with_user_assistant_result(tmp_path):
    sess = tmp_path / "session.jsonl"
    sess.write_text(
        '{"type":"user","text":"do thing"}\n'
        '{"type":"assistant","text":"thinking"}\n'
        '{"type":"result","session_id":"abc","usage":{"input_tokens":10,"output_tokens":5}}\n'
    )
    return tmp_path


def test_format_session_tail_renders_user_tag(_session_with_user_assistant_result):
    # Arrange
    sess_dir = _session_with_user_assistant_result
    # Act
    out = _format_session_tail(sess_dir, max_lines=10)
    # Assert
    assert "[user]" in out


def test_format_session_tail_renders_user_text(_session_with_user_assistant_result):
    # Arrange
    sess_dir = _session_with_user_assistant_result
    # Act
    out = _format_session_tail(sess_dir, max_lines=10)
    # Assert
    assert "do thing" in out


def test_format_session_tail_renders_assistant_tag(_session_with_user_assistant_result):
    # Arrange
    sess_dir = _session_with_user_assistant_result
    # Act
    out = _format_session_tail(sess_dir, max_lines=10)
    # Assert
    assert "[assistant]" in out


def test_format_session_tail_renders_assistant_text(
    _session_with_user_assistant_result,
):
    # Arrange
    sess_dir = _session_with_user_assistant_result
    # Act
    out = _format_session_tail(sess_dir, max_lines=10)
    # Assert
    assert "thinking" in out


def test_format_session_tail_renders_result_tag(_session_with_user_assistant_result):
    # Arrange
    sess_dir = _session_with_user_assistant_result
    # Act
    out = _format_session_tail(sess_dir, max_lines=10)
    # Assert
    assert "[result]" in out


def test_format_session_tail_renders_session_id(_session_with_user_assistant_result):
    # Arrange
    sess_dir = _session_with_user_assistant_result
    # Act
    out = _format_session_tail(sess_dir, max_lines=10)
    # Assert
    assert "session=abc" in out


def test_format_session_tail_renders_input_tokens(_session_with_user_assistant_result):
    # Arrange
    sess_dir = _session_with_user_assistant_result
    # Act
    out = _format_session_tail(sess_dir, max_lines=10)
    # Assert
    assert "in=10" in out


def test_format_session_tail_renders_output_tokens(_session_with_user_assistant_result):
    # Arrange
    sess_dir = _session_with_user_assistant_result
    # Act
    out = _format_session_tail(sess_dir, max_lines=10)
    # Assert
    assert "out=5" in out


def test_format_session_tail_returns_empty_when_missing(tmp_path):
    # Arrange
    # (no session.jsonl created)
    # Act
    out = _format_session_tail(tmp_path, max_lines=10)
    # Assert
    assert out == ""


@pytest.fixture
def _session_with_twenty_chunks(tmp_path):
    sess = tmp_path / "session.jsonl"
    sess.write_text(
        "\n".join(f'{{"type":"assistant","text":"chunk-{i}"}}' for i in range(20))
        + "\n"
    )
    return tmp_path


def test_format_session_tail_returns_exactly_max_lines(_session_with_twenty_chunks):
    # Arrange
    sess_dir = _session_with_twenty_chunks
    # Act
    out = _format_session_tail(sess_dir, max_lines=3)
    # Assert
    assert out.count("[assistant]") == 3


def test_format_session_tail_keeps_last_chunk(_session_with_twenty_chunks):
    # Arrange
    sess_dir = _session_with_twenty_chunks
    # Act
    out = _format_session_tail(sess_dir, max_lines=3)
    # Assert
    assert "chunk-19" in out


def test_format_session_tail_drops_first_chunk(_session_with_twenty_chunks):
    # Arrange
    sess_dir = _session_with_twenty_chunks
    # Act
    out = _format_session_tail(sess_dir, max_lines=3)
    # Assert
    assert "chunk-0" not in out


# ---------------------------------------------------------------------------
# Legacy dot_claude/ layout was removed (ADR-0006): a spec that still
# ships a dot_claude/ dir hard-errors pointing at to_home/.
# ---------------------------------------------------------------------------


def test_legacy_dot_claude_dir_raises_runtime_error(tmp_path):
    # Arrange
    agent_dir = tmp_path / "agent"
    (agent_dir / "dot_claude").mkdir(parents=True)
    config = AgentConfig(name="legacy", config_path=str(agent_dir / "spec.yaml"))
    home_dir = str(tmp_path / "home")
    # Act
    raises_cm = pytest.raises(RuntimeError)
    # Assert
    with raises_cm:
        _materialize_home_layouts(config, home_dir)


def test_legacy_dot_claude_error_points_at_to_home(tmp_path):
    # Arrange
    agent_dir = tmp_path / "agent"
    (agent_dir / "dot_claude").mkdir(parents=True)
    config = AgentConfig(name="legacy", config_path=str(agent_dir / "spec.yaml"))
    try:
        _materialize_home_layouts(config, str(tmp_path / "home"))
        message = ""
    except RuntimeError as exc:
        message = str(exc)
    # Act
    points_at_to_home = "to_home/" in message
    # Assert
    assert points_at_to_home is True


def test_to_home_dir_materializes_without_error(tmp_path):
    # Arrange
    agent_dir = tmp_path / "agent"
    to_home = agent_dir / "to_home"
    to_home.mkdir(parents=True)
    (to_home / ".mcp.json").write_text('{"mcpServers": {}}\n')
    config = AgentConfig(name="modern", config_path=str(agent_dir / "spec.yaml"))
    home_dir = tmp_path / "home"
    # Act
    _materialize_home_layouts(config, str(home_dir))
    # Assert
    assert (home_dir / ".mcp.json").exists()
