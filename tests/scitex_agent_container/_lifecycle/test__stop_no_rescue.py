"""The stop path NEVER commits on the agent's behalf (pre-stop rescue abolished).

Operator ruling 2026-07-19 (「rescue 一切やめましょう」): the fleet-default
pre-stop rescue is GONE. Stopping an agent must leave a dirty worktree
exactly as dirty as it found it.

WHY THE RESCUE HAD TO GO — its own "never publishes" contract
(``_pre_stop_rescue_git``: "no code path can publish on the agent's
behalf") was enforced against the WRONG VERB. It blocked ``push`` but
not ``merge``: on a NON-protected topic branch the rescue committed IN
PLACE, that branch later became a PR, and the rescue commit rode a
legitimate merge into ``develop``. Rescue commits are reachable from
``origin/develop`` today. One of them (``37d83977``) carried nine ``mode
160000`` gitlinks with no ``.gitmodules`` and broke ``actions/checkout``
on every workflow run until PR #769 removed them.

The regression guard below is the TOPIC-BRANCH case specifically,
because that is the case the old code got wrong: a protected branch was
already routed to a side-branch, so a test on ``develop`` would have
passed against the buggy code and proved nothing.

STX-TQ002 AAA + STX-TQ007 one-assert. No mocks for the thing under test
— a REAL ``tmp_path`` git repo driven through REAL ``git``. The runtime
is a recording double because the alternative is launching apptainer.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Iterator

import pytest

from scitex_agent_container._lifecycle import lifecycle as lc
from scitex_agent_container._state.registry import Registry


@pytest.fixture(autouse=True)
def _isolate_home_and_git_identity(tmp_path: Path) -> Iterator[None]:
    """Sandbox ``HOME`` and pin a git identity — explicit save/restore, no monkeypatch.

    ``HOME`` is re-pointed so the agent's state dir and any per-user git
    state land under ``tmp_path``. The author/committer pins keep the
    test independent of the runner's global ``~/.gitconfig`` (CI runners
    often have none, and ``git commit`` then fails for the wrong reason).
    """
    keys = [
        "HOME",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ]
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["HOME"] = str(tmp_path)
    for key, value in [
        ("GIT_AUTHOR_NAME", "Stop Tester"),
        ("GIT_AUTHOR_EMAIL", "stop@example.invalid"),
        ("GIT_COMMITTER_NAME", "Stop Tester"),
        ("GIT_COMMITTER_EMAIL", "stop@example.invalid"),
    ]:
        os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(registry_dir=tmp_path / "reg")


class _FakeRuntime:
    """Recording runtime double; ``stop`` really stops it (is_running → False)."""

    def __init__(self, *, running: bool = True) -> None:
        self.running = running
        self.stop_calls: list[Any] = []

    def is_running(self, config: Any) -> bool:
        return self.running

    def start(self, config: Any, **_kw: Any) -> bool:
        self.running = True
        return True

    def stop(self, config: Any) -> None:
        self.stop_calls.append(config)
        self.running = False

    def logs(self, config: Any, lines: int) -> str:
        return ""


class _FakeHandover:
    def ensure_instance_uuid(self, c: Any) -> str:
        return "uuid"

    def hydrate_from_hub(self, c: Any) -> bool:
        return True

    def push_pre_stop_snapshot(self, c: Any, payload: Any = None) -> bool:
        return True

    def start_failback_poller(self, c: Any) -> None:
        return None


def _git(args: list[str], *, cwd: Path) -> None:
    """Run ``git <args>``; raise on non-zero so setup fails loud."""
    subprocess.check_call(["git", *args], cwd=str(cwd))


def _git_out(args: list[str], *, cwd: Path) -> str:
    """Run ``git <args>`` and return stripped stdout (raises on non-zero)."""
    return subprocess.check_output(["git", *args], cwd=str(cwd), text=True).strip()


def _make_dirty_worktree(root: Path, *, branch: str = "feature/topic") -> Path:
    """A real git repo on a NON-protected topic branch with an UNCOMMITTED change.

    The topic branch is load-bearing: it is the exact case the abolished
    rescue committed in place, and therefore the case whose commit rode a
    later PR merge into ``develop``.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "--initial-branch=" + branch, "--quiet"], cwd=root)
    (root / "README.md").write_text("# work\n")
    _git(["add", "README.md"], cwd=root)
    _git(["commit", "-m", "init", "--quiet"], cwd=root)
    (root / "README.md").write_text("# work\n# uncommitted agent work\n")
    return root


def _write_spec(tmp_path: Path, workdir: Path, name: str = "alpha") -> Path:
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    body = (
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  host: ${HOSTNAME}\n"
        f"  workdir: {workdir}\n"
        "  apptainer:\n"
        "    image: /x.sif\n"
        "    binds: []\n"
        "  claude:\n"
        "    model: sonnet\n"
        "  health:\n"
        "    enabled: false\n"
        "    interval: 60\n"
        "  restart:\n"
        "    policy: on-failure\n"
        "    max_retries: 3\n"
        "  hooks:\n"
        "    pre_start: []\n"
        "    post_start: []\n"
        "    pre_stop: []\n"
        "    post_stop: []\n"
    )
    spec = agent_dir / "spec.yaml"
    spec.write_text(body)
    return spec


# ---------------------------------------------------------------------------
# The regression guard — stopping must not commit
# ---------------------------------------------------------------------------


def test_agent_stop_leaves_head_unmoved_on_dirty_topic_branch_worktree(
    tmp_path: Path, registry: Registry
) -> None:
    """Stopping an agent whose workdir is dirty must create NO commit.

    RED against the pre-removal code: the rescue committed the dirty tree
    in place on a topic branch, moving HEAD.
    """
    # Arrange — a live agent whose workdir is a dirty topic-branch repo.
    workdir = _make_dirty_worktree(tmp_path / "work")
    spec = _write_spec(tmp_path, workdir)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = _FakeRuntime(running=True)
    head_before = _git_out(["rev-parse", "HEAD"], cwd=workdir)
    # Act
    lc.agent_stop(
        "alpha",
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=_FakeHandover(),
    )
    # Assert — HEAD did not move: nothing was committed on the agent's behalf.
    assert _git_out(["rev-parse", "HEAD"], cwd=workdir) == head_before


def test_agent_stop_leaves_dirty_worktree_still_dirty(
    tmp_path: Path, registry: Registry
) -> None:
    """The uncommitted change survives the stop AS uncommitted work.

    Complements the HEAD assertion: HEAD could also stay put if the
    change had been discarded. It must still be there, still dirty.
    """
    # Arrange
    workdir = _make_dirty_worktree(tmp_path / "work")
    spec = _write_spec(tmp_path, workdir)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = _FakeRuntime(running=True)
    # Act
    lc.agent_stop(
        "alpha",
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=_FakeHandover(),
    )
    # Assert — porcelain still reports the file as UNSTAGED-modified.
    # Raw porcelain is " M README.md" (index clean, worktree modified);
    # ``_git_out`` strips, leaving "M README.md".
    assert _git_out(["status", "--porcelain"], cwd=workdir) == "M README.md"


def test_agent_stop_creates_no_rescue_branch_for_dirty_worktree(
    tmp_path: Path, registry: Registry
) -> None:
    """No ``rescue/`` side-branch is created either — the whole mechanism is gone."""
    # Arrange
    workdir = _make_dirty_worktree(tmp_path / "work")
    spec = _write_spec(tmp_path, workdir)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = _FakeRuntime(running=True)
    # Act
    lc.agent_stop(
        "alpha",
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=_FakeHandover(),
    )
    # Assert — the branch list contains no rescue/ ref.
    assert "rescue/" not in _git_out(["branch", "--list"], cwd=workdir)


# ---------------------------------------------------------------------------
# CONTROL — the stop itself must still work
# ---------------------------------------------------------------------------


def test_agent_stop_still_stops_the_runtime_with_a_dirty_worktree(
    tmp_path: Path, registry: Registry
) -> None:
    """CONTROL: without this, "no commit" could pass by breaking stop entirely."""
    # Arrange
    workdir = _make_dirty_worktree(tmp_path / "work")
    spec = _write_spec(tmp_path, workdir)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = _FakeRuntime(running=True)
    # Act
    lc.agent_stop(
        "alpha",
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=_FakeHandover(),
    )
    # Assert — the runtime was actually torn down.
    assert runtime.running is False


def test_agent_stop_returns_true_with_a_dirty_worktree(
    tmp_path: Path, registry: Registry
) -> None:
    """CONTROL: the stop reports success; a dirty tree is not an error condition."""
    # Arrange
    workdir = _make_dirty_worktree(tmp_path / "work")
    spec = _write_spec(tmp_path, workdir)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = _FakeRuntime(running=True)
    # Act
    result = lc.agent_stop(
        "alpha",
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=_FakeHandover(),
    )
    # Assert
    assert result is True
