"""Spec-pinned session-id seeding (``session: resume`` + ``resume_id``).

Real behaviour, no mocks of the code under test: a real on-disk state
dir, a real runtime stub exposing ``_state_dir`` (mirrors
ApptainerContainerRuntime's API), and the real ``read_session_id`` /
``write_session_id`` marker helpers.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._lifecycle._session_seed import seed_pinned_session_id
from scitex_agent_container._runners._session_state import (
    read_session_id,
    read_session_id_history,
    write_session_id,
)
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ClaudeSpec

_VALID_UUID = "123e4567-e89b-12d3-a456-426614174000"
_FORKED_UUID = "abcdef01-2345-6789-abcd-ef0123456789"


class _RuntimeStub:
    """Honest runtime collaborator — only the ``_state_dir`` resolver."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _state_dir(self, config: AgentConfig) -> Path:
        return self._root / config.name


def _cfg(name: str, *, session: str, resume_id: str = "") -> AgentConfig:
    return AgentConfig(
        name=name,
        runtime="apptainer",
        claude=ClaudeSpec(model="haiku", session=session, resume_id=resume_id),
    )


def test_seed_returns_true_when_marker_absent(tmp_path: Path):
    # Arrange — first boot: no session_id marker yet.
    cfg = _cfg("seed-a", session="resume", resume_id=_VALID_UUID)
    # Act
    seeded = seed_pinned_session_id(cfg, _RuntimeStub(tmp_path))
    # Assert
    assert seeded is True


def test_seed_writes_pinned_uuid_as_marker(tmp_path: Path):
    # Arrange
    cfg = _cfg("seed-a", session="resume", resume_id=_VALID_UUID)
    # Act
    seed_pinned_session_id(cfg, _RuntimeStub(tmp_path))
    # Assert
    assert read_session_id(tmp_path / "seed-a") == _VALID_UUID


def test_seed_records_pinned_uuid_in_history(tmp_path: Path):
    # Arrange
    cfg = _cfg("seed-a", session="resume", resume_id=_VALID_UUID)
    # Act
    seed_pinned_session_id(cfg, _RuntimeStub(tmp_path))
    # Assert
    assert read_session_id_history(tmp_path / "seed-a") == [_VALID_UUID]


def test_seed_returns_false_when_marker_exists(tmp_path: Path):
    # Arrange — a prior turn already forked the id; the marker is the fork.
    cfg = _cfg("seed-b", session="resume", resume_id=_VALID_UUID)
    write_session_id(tmp_path / "seed-b", _FORKED_UUID)
    # Act
    seeded = seed_pinned_session_id(cfg, _RuntimeStub(tmp_path))
    # Assert
    assert seeded is False


def test_seed_preserves_existing_forked_marker(tmp_path: Path):
    # Arrange — re-seeding must NOT clobber the fork back to the pin.
    cfg = _cfg("seed-b", session="resume", resume_id=_VALID_UUID)
    write_session_id(tmp_path / "seed-b", _FORKED_UUID)
    # Act
    seed_pinned_session_id(cfg, _RuntimeStub(tmp_path))
    # Assert
    assert read_session_id(tmp_path / "seed-b") == _FORKED_UUID


def test_seed_noop_when_session_not_resume(tmp_path: Path):
    # Arrange — resume_id set but session is not "resume".
    cfg = _cfg("seed-c", session="continue", resume_id=_VALID_UUID)
    # Act
    seeded = seed_pinned_session_id(cfg, _RuntimeStub(tmp_path))
    # Assert
    assert seeded is False


def test_seed_noop_when_no_resume_id(tmp_path: Path):
    # Arrange — session=resume but no explicit id (not pinned).
    cfg = _cfg("seed-d", session="resume", resume_id="")
    # Act
    seeded = seed_pinned_session_id(cfg, _RuntimeStub(tmp_path))
    # Assert
    assert seeded is False
