"""Tests for the operator-facing ``--resume`` preflight (#192, Part B #3).

When the operator explicitly asks ``--resume <uuid>`` and the id is gone,
the preflight must FAIL LOUD + INFORMATIVE (list resumable conversations)
rather than silently fresh-start. These tests prove the raise + its
candidate-listing body, and that a valid id passes silently.

No-mocks: real on-disk projects store under a tmp runtime dir + a real
``AgentConfig``. Conforms to STX-TQ002 (AAA markers), STX-TQ003
(descriptive names), STX-TQ007 (one assertion per test).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container._runners._session_candidates import (
    encode_claude_project,
)
from scitex_agent_container.cli_pkg.lifecycle._resume_preflight import (
    ResumePreflightError,
    preflight_resume_id,
)


@pytest.fixture
def runtime_root(tmp_path: Path, env_save_restore):
    """Isolate the runtime root so the container-home lookup is tmp-scoped."""
    root = tmp_path / "runtime"
    root.mkdir()
    env_save_restore.set("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(root))
    return root


class _FakeApptainer:
    def __init__(self, container_workdir: str) -> None:
        self.container_workdir = container_workdir


class _FakeHostsSpec:
    def __init__(self, host=None) -> None:
        self.host = host


class _FakeConfig:
    """Minimal AgentConfig stand-in carrying only what the preflight reads.

    Not a mock — a real production-shaped value object with the attributes
    ``preflight_resume_id`` touches (``name``, ``apptainer.container_workdir``,
    and — since the access-posture refactor — ``access`` + ``workdir``, which
    ``resolve_pwd`` consults to key the conversation store on the cwd the
    inner ``claude`` actually runs at).

    ``access`` defaults to ``"capsule"`` here so the resolved ``--pwd`` is the
    ``container_workdir`` these tests seed their transcripts under. The
    full-access ``--pwd`` (canonical ``workdir``) path is covered separately
    below.
    """

    def __init__(
        self,
        name: str,
        container_workdir: str,
        *,
        access: str = "capsule",
        workdir: str | None = None,
    ) -> None:
        self.name = name
        self.apptainer = _FakeApptainer(container_workdir)
        self.hosts_spec = _FakeHostsSpec()
        self.access = access
        # Canonical host workdir (used only by the full-access --pwd path).
        self.workdir = workdir if workdir is not None else container_workdir


def _seed_conversation(
    runtime_root: Path,
    name: str,
    container_workdir: str,
    session_id: str,
) -> Path:
    """Write a real transcript under runtime/<name>/home/.claude/projects/."""
    home = runtime_root / name / "home"
    proj = home / ".claude" / "projects" / encode_claude_project(container_workdir)
    proj.mkdir(parents=True, exist_ok=True)
    p = proj / f"{session_id}.jsonl"
    p.write_text(
        json.dumps({"type": "user", "message": {"content": "prior work"}}) + "\n",
        encoding="utf-8",
    )
    return p


class TestPreflightResumeId:
    def test_valid_resume_id_passes_silently(self, runtime_root: Path) -> None:
        # Arrange — a transcript whose id the operator asks to resume.
        cfg = _FakeConfig("clew", "/home/agent/work")
        _seed_conversation(runtime_root, "clew", "/home/agent/work", "uuid-live")
        # Act — returns None (no raise) for a valid id.
        result = preflight_resume_id(cfg, "uuid-live")
        # Assert
        assert result is None

    def test_unknown_resume_id_raises_preflight_error(self, runtime_root: Path) -> None:
        # Arrange — store holds a DIFFERENT id than requested.
        cfg = _FakeConfig("clew", "/home/agent/work")
        _seed_conversation(runtime_root, "clew", "/home/agent/work", "uuid-live")
        # Act
        ctx = pytest.raises(ResumePreflightError)
        # Assert
        with ctx:
            preflight_resume_id(cfg, "uuid-gone")

    def test_error_lists_the_resumable_candidate(self, runtime_root: Path) -> None:
        # Arrange
        cfg = _FakeConfig("clew", "/home/agent/work")
        _seed_conversation(runtime_root, "clew", "/home/agent/work", "uuid-live")
        # Act
        ctx = pytest.raises(ResumePreflightError, match="uuid-live")
        # Assert — the informative body names the resumable conversation.
        with ctx:
            preflight_resume_id(cfg, "uuid-gone")

    def test_error_names_explicit_fresh_start_next_step(
        self, runtime_root: Path
    ) -> None:
        # Arrange
        cfg = _FakeConfig("clew", "/home/agent/work")
        _seed_conversation(runtime_root, "clew", "/home/agent/work", "uuid-live")
        # Act
        ctx = pytest.raises(ResumePreflightError, match="new-session")
        # Assert — points at the EXPLICIT last-resort fresh start.
        with ctx:
            preflight_resume_id(cfg, "uuid-gone")

    def test_no_candidates_at_all_still_raises_loud(self, runtime_root: Path) -> None:
        # Arrange — container home exists but holds no transcripts.
        cfg = _FakeConfig("clew", "/home/agent/work")
        (runtime_root / "clew" / "home").mkdir(parents=True)
        # Act
        ctx = pytest.raises(ResumePreflightError, match="no resumable conversations")
        # Assert
        with ctx:
            preflight_resume_id(cfg, "uuid-gone")

    def test_remote_agent_warns_and_returns_without_raising(
        self, runtime_root: Path, capsys
    ) -> None:
        # Arrange — a remote agent's store is not on this host.
        cfg = _FakeConfig("clew", "/home/agent/work")
        # Act — is_remote short-circuits to a loud warning, no raise.
        result = preflight_resume_id(cfg, "uuid-gone", is_remote=True)
        # Assert
        assert result is None

    def test_unmaterialised_home_warns_and_returns_without_raising(
        self, runtime_root: Path
    ) -> None:
        # Arrange — no runtime/<name>/home dir yet (first-ever start).
        cfg = _FakeConfig("clew", "/home/agent/work")
        # Act — degrades to a loud warning rather than a hard block.
        result = preflight_resume_id(cfg, "uuid-gone")
        # Assert
        assert result is None

    def test_full_access_keys_store_on_canonical_workdir_not_alias(
        self, runtime_root: Path
    ) -> None:
        # Arrange — a full-access agent: the inner claude runs at the
        # CANONICAL workdir (--pwd), so its transcripts live under the
        # canonical-path encoding, NOT the /work alias. Seed there and prove
        # the preflight resolves the valid id (would falsely fail if it still
        # keyed on container_workdir == /work).
        cfg = _FakeConfig(
            "fa",
            "/work",
            access="full",
            workdir="/home/ywatanabe/proj/figrecipe",
        )
        _seed_conversation(
            runtime_root, "fa", "/home/ywatanabe/proj/figrecipe", "uuid-canon"
        )
        # Act
        result = preflight_resume_id(cfg, "uuid-canon")
        # Assert
        assert result is None
