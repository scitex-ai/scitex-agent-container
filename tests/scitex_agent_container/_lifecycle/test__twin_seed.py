"""Twin host-side seed — the boot-time half (``_lifecycle._twin_seed``).

Real behaviour, no mocks of the code under test: ``seed_twin_from_parent``
is exercised against a real on-disk parent spec (resolved via the real config
resolver), real per-agent state dirs, a real transcript file, and the real
``read_session_id`` / ``write_session_id`` marker helpers. The only stub is an
honest runtime collaborator exposing ``_state_dir`` (mirrors
ApptainerContainerRuntime's resolver API), same as the session-seed suite.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._twin import (
    TODO_AGENT_ENV,
    TWIN_PARENT_ENV,
    TwinIdentityError,
    TwinSeedError,
    seed_twin_from_parent,
    twin_session_uuid,
)
from scitex_agent_container._runners._session_state import (
    read_session_id,
    write_session_id,
)
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ClaudeSpec

_UUID = "123e4567-e89b-12d3-a456-426614174000"

# ─── seed_twin_from_parent (real on-disk parent + state dirs) ─────────────


class _RuntimeStub:
    """Honest runtime collaborator — only the ``_state_dir`` resolver."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _state_dir(self, config: AgentConfig) -> Path:
        return self._root / config.name


def _write_parent_spec(agents_dir: Path, name: str) -> None:
    spec_dir = agents_dir / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.yaml").write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        # ${HOSTNAME} is the validator-documented portable-fixture form:
        # 'host: local' is BANNED (operator directive 2026-07-10) and a
        # hardcoded hostname would break on any other machine (incl. CI).
        "  host: ${HOSTNAME}\n"
        "  workdir: /home/agent/proj/x\n"
        "  apptainer:\n"
        "    image: /x.sif\n"
        "    binds: []\n"
        "  claude:\n"
        "    model: haiku\n"
        "  health:\n"
        "    enabled: true\n"
        "    interval: 30\n"
        "    method: sdk-alive\n"
        "  restart:\n"
        "    policy: never\n"
        "    max_retries: 0\n",
        encoding="utf-8",
    )


def _seed_parent_session(state_root: Path, name: str, uuid: str) -> Path:
    """Write the parent's session_id marker + a transcript; return the jsonl."""
    write_session_id(state_root / name, uuid)
    proj = state_root / name / "home" / ".claude" / "projects" / "-home-agent-proj-x"
    proj.mkdir(parents=True, exist_ok=True)
    jsonl = proj / f"{uuid}.jsonl"
    jsonl.write_text('{"type":"user","message":{"content":"hi"}}\n', encoding="utf-8")
    return jsonl


def _twin_cfg(parent: str, twin: str) -> AgentConfig:
    """A twin config shaped like one ``derive_twin_spec`` actually produces.

    BOTH identity vars are present: ``derive_twin_spec`` always writes
    ``SCITEX_TODO_AGENT_ID`` (author = twin) alongside ``SAC_TWIN_PARENT``
    into the same ``spec.apptainer.env`` block, and the loader merges that
    block into ``config.env``. A twin carrying only the parent var cannot
    exist in production — it is the malformed case ``assert_twin_identity``
    exists to REFUSE, and it is asserted on directly in the identity-gate
    tests below.
    """
    return AgentConfig(
        name=twin,
        runtime="apptainer",
        claude=ClaudeSpec(model="haiku", session="resume"),
        env={TWIN_PARENT_ENV: parent, TODO_AGENT_ENV: twin},
    )


@pytest.fixture()
def _set_yaml_dirs():
    """Yield a setter for ``SCITEX_AGENT_CONTAINER_YAML_DIRS`` (restored on teardown).

    Real env manipulation (no ``monkeypatch``, forbidden ecosystem-wide):
    saves the prior value, hands the test a setter, and restores on teardown
    so the real config resolver finds the tmp parent spec during the test.
    """
    key = "SCITEX_AGENT_CONTAINER_YAML_DIRS"
    prev = os.environ.get(key)

    def _set(path) -> None:
        os.environ[key] = str(path)

    yield _set
    if prev is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = prev


@pytest.fixture()
def _twin_env(tmp_path, _set_yaml_dirs):
    """Real parent spec on disk + a parent live session; returns the pieces."""
    agents_dir = tmp_path / "agents"
    state_root = tmp_path / "state"
    _write_parent_spec(agents_dir, "twinp")
    jsonl = _seed_parent_session(state_root, "twinp", _UUID)
    _set_yaml_dirs(agents_dir)
    return state_root, jsonl


def test_seed_noop_for_non_twin(tmp_path):
    # Arrange — a config with no SAC_TWIN_PARENT is not a twin.
    cfg = AgentConfig(name="plain", runtime="apptainer")
    # Act
    seeded = seed_twin_from_parent(cfg, _RuntimeStub(tmp_path))
    # Assert
    assert seeded is False


def test_seed_returns_true_for_twin(_twin_env):
    # Arrange
    state_root, _ = _twin_env
    # Act
    seeded = seed_twin_from_parent(
        _twin_cfg("twinp", "twinp-twin"), _RuntimeStub(state_root)
    )
    # Assert
    assert seeded is True


def test_seed_copies_transcript_into_twin_home(_twin_env):
    # Arrange
    state_root, _ = _twin_env
    # Act
    seed_twin_from_parent(_twin_cfg("twinp", "twinp-twin"), _RuntimeStub(state_root))
    # Assert
    assert (
        state_root
        / "twinp-twin"
        / "home"
        / ".claude"
        / "projects"
        / "-home-agent-proj-x"
        / f"{_UUID}.jsonl"
    ).is_file()


def test_seed_marks_twin_session_id_to_parent_uuid(_twin_env):
    # Arrange
    state_root, _ = _twin_env
    # Act
    seed_twin_from_parent(_twin_cfg("twinp", "twinp-twin"), _RuntimeStub(state_root))
    # Assert
    assert read_session_id(state_root / "twinp-twin") == _UUID


def test_seed_noop_when_twin_already_booted(_twin_env):
    # Arrange — the twin has its OWN (diverged) session marker already.
    state_root, _ = _twin_env
    own = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    write_session_id(state_root / "twinp-twin", own)
    # Act
    seeded = seed_twin_from_parent(
        _twin_cfg("twinp", "twinp-twin"), _RuntimeStub(state_root)
    )
    # Assert
    assert seeded is False


def test_seed_preserves_diverged_twin_marker_on_restart(_twin_env):
    # Arrange — a restart must never re-fork the twin back to the parent uuid.
    state_root, _ = _twin_env
    own = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    write_session_id(state_root / "twinp-twin", own)
    # Act
    seed_twin_from_parent(_twin_cfg("twinp", "twinp-twin"), _RuntimeStub(state_root))
    # Assert
    assert read_session_id(state_root / "twinp-twin") == own


def test_seed_fails_loud_when_parent_has_no_session(tmp_path, _set_yaml_dirs):
    # Arrange — parent spec exists but no session_id marker was written.
    agents_dir = tmp_path / "agents"
    _write_parent_spec(agents_dir, "twinp")
    _set_yaml_dirs(agents_dir)
    cfg = _twin_cfg("twinp", "twinp-twin")
    runtime = _RuntimeStub(tmp_path / "state")

    def _run() -> None:
        seed_twin_from_parent(cfg, runtime)

    # Act
    raised = pytest.raises(TwinSeedError)
    # Assert
    with raised:
        _run()


def test_seed_points_first_boot_at_parent_session_with_fork(_twin_env):
    # Arrange — first boot: the launch must RESUME the parent's uuid.
    state_root, _ = _twin_env
    cfg = _twin_cfg("twinp", "twinp-twin")
    # Act
    seed_twin_from_parent(cfg, _RuntimeStub(state_root))
    # Assert
    assert cfg.claude.resume_id == _UUID


def test_seed_sets_fork_session_on_first_boot(_twin_env):
    # Arrange — inherit the conversation, but not the parent's session id.
    state_root, _ = _twin_env
    cfg = _twin_cfg("twinp", "twinp-twin")
    # Act
    seed_twin_from_parent(cfg, _RuntimeStub(state_root))
    # Assert
    assert cfg.claude.fork_session is True


def test_seed_forks_into_deterministic_session_uuid(_twin_env):
    # Arrange — the forked-into id is derived from the twin's NAME.
    state_root, _ = _twin_env
    cfg = _twin_cfg("twinp", "twinp-twin")
    # Act
    seed_twin_from_parent(cfg, _RuntimeStub(state_root))
    # Assert
    assert cfg.claude.session_id == twin_session_uuid("twinp-twin")


def test_seed_fork_target_is_not_the_parent_session(_twin_env):
    # Arrange — the whole point: the twin must not adopt the parent's id.
    state_root, _ = _twin_env
    cfg = _twin_cfg("twinp", "twinp-twin")
    # Act
    seed_twin_from_parent(cfg, _RuntimeStub(state_root))
    # Assert
    assert cfg.claude.session_id != _UUID


def test_seed_fails_loud_when_transcript_missing(tmp_path, _set_yaml_dirs):
    # Arrange — parent has a session id but no transcript file on disk.
    agents_dir = tmp_path / "agents"
    state_root = tmp_path / "state"
    _write_parent_spec(agents_dir, "twinp")
    write_session_id(state_root / "twinp", _UUID)
    _set_yaml_dirs(agents_dir)
    cfg = _twin_cfg("twinp", "twinp-twin")
    runtime = _RuntimeStub(state_root)

    def _run() -> None:
        seed_twin_from_parent(cfg, runtime)

    # Act
    raised = pytest.raises(TwinSeedError)
    # Assert
    with raised:
        _run()


def test_seed_refuses_malformed_identity_before_seeding(_twin_env):
    # Arrange — the gate runs BEFORE the first-boot work, on every boot.
    state_root, _ = _twin_env
    cfg = AgentConfig(
        name="twinp-twin",
        runtime="apptainer",
        claude=ClaudeSpec(model="haiku", session="resume"),
        env={TWIN_PARENT_ENV: "twinp", TODO_AGENT_ENV: "twinp"},
    )

    def _run() -> None:
        seed_twin_from_parent(cfg, _RuntimeStub(state_root))

    # Act
    raised = pytest.raises(TwinIdentityError)
    # Assert
    with raised:
        _run()


