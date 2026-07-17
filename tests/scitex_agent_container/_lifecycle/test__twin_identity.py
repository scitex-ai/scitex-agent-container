"""Twin identity — naming algebra + boot gate (``_lifecycle._twin_identity``).

Real behaviour, no mocks: pure functions over real values, and the boot gate
asserted against real ``AgentConfig`` objects (including the malformed shapes
it exists to REFUSE).
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._twin import (
    TODO_AGENT_ENV,
    TWIN_PARENT_ENV,
    TwinIdentityError,
    TwinSeedError,
    assert_twin_identity,
    resolve_twin_name,
    twin_name_for_tag,
    twin_session_uuid,
)
from scitex_agent_container.config import AgentConfig


def _twin_cfg(parent: str, twin: str) -> AgentConfig:
    """A twin config shaped like one ``derive_twin_spec`` actually produces."""
    return AgentConfig(
        name=twin,
        runtime="apptainer",
        env={TWIN_PARENT_ENV: parent, TODO_AGENT_ENV: twin},
    )


# ─── resolve_twin_name ────────────────────────────────────────────────────


def test_resolve_twin_name_defaults_to_parent_twin():
    # Arrange
    parent = "neurovista"
    # Act
    name = resolve_twin_name(parent, None, [])
    # Assert
    assert name == "neurovista-twin"


def test_resolve_twin_name_honours_explicit_request():
    # Arrange
    requested = "neurovista-writer"
    # Act
    name = resolve_twin_name("neurovista", requested, ["neurovista-writer"])
    # Assert
    assert name == "neurovista-writer"


# ─── --tag: deterministic naming ──────────────────────────────────────────


def test_tag_derives_forked_name():
    # Arrange
    parent = "scitex-agent-container"
    # Act
    name = twin_name_for_tag(parent, "review-pr-712")
    # Assert
    assert name == "scitex-agent-container-forked-review-pr-712"


def test_tag_name_is_deterministic_across_calls():
    # Arrange — the anti-sprawl property: same tag => same id, never -2.
    first = resolve_twin_name("sac", None, [], tag="triage")
    # Act
    second = resolve_twin_name("sac", None, ["sac-forked-triage"], tag="triage")
    # Assert
    assert first == second == "sac-forked-triage"


def test_untagged_default_still_bumps_for_back_compat():
    # Arrange
    existing = ["sac-twin"]
    # Act
    name = resolve_twin_name("sac", None, existing)
    # Assert
    assert name == "sac-twin-2"


def test_tag_and_name_are_mutually_exclusive():
    # Arrange
    def _run() -> None:
        resolve_twin_name("sac", "explicit", [], tag="triage")

    # Act
    raised = pytest.raises(TwinSeedError)
    # Assert
    with raised:
        _run()


@pytest.mark.parametrize("bad", ["Review PR", "review/pr", "-lead", "rev--iew", ""])
def test_tag_rejects_non_slug(bad):
    # Arrange — the id becomes a dir / a2a address / tmux session name.
    def _run() -> None:
        twin_name_for_tag("sac", bad)

    # Act
    raised = pytest.raises(TwinSeedError)
    # Assert
    with raised:
        _run()


def test_tag_rejects_overlong_slug():
    # Arrange
    def _run() -> None:
        twin_name_for_tag("sac", "a" * 41)

    # Act
    raised = pytest.raises(TwinSeedError)
    # Assert
    with raised:
        _run()


# ─── deterministic session uuid ───────────────────────────────────────────


def test_session_uuid_is_deterministic():
    # Arrange
    name = "sac-forked-triage"
    # Act
    first = twin_session_uuid(name)
    second = twin_session_uuid(name)
    # Assert
    assert first == second


def test_session_uuid_differs_per_twin():
    # Arrange
    triage, review = "sac-forked-triage", "sac-forked-review"
    # Act
    a, b = twin_session_uuid(triage), twin_session_uuid(review)
    # Assert
    assert a != b


def test_session_uuid_is_a_valid_uuid():
    # Arrange — `claude --session-id <uuid>` requires a well-formed UUID.
    import uuid as _uuid

    value = twin_session_uuid("sac-forked-triage")
    # Act
    parsed = _uuid.UUID(value)
    # Assert
    assert str(parsed) == value


# ─── boot identity gate ───────────────────────────────────────────────────


def test_identity_gate_noop_for_non_twin():
    # Arrange
    cfg = AgentConfig(name="plain", runtime="apptainer", env={})
    # Act
    checked = assert_twin_identity(cfg)
    # Assert
    assert checked is False


def test_identity_gate_passes_for_well_formed_twin():
    # Arrange
    cfg = _twin_cfg("parent", "parent-forked-triage")
    # Act
    checked = assert_twin_identity(cfg)
    # Assert
    assert checked is True


def test_identity_gate_refuses_when_author_id_missing():
    # Arrange — no SCITEX_TODO_AGENT_ID: writes would fall back to ambient.
    cfg = AgentConfig(
        name="parent-forked-triage",
        runtime="apptainer",
        env={TWIN_PARENT_ENV: "parent"},
    )

    def _run() -> None:
        assert_twin_identity(cfg)

    # Act
    raised = pytest.raises(TwinIdentityError)
    # Assert
    with raised:
        _run()


def test_identity_gate_refuses_when_author_id_is_the_parent():
    # Arrange — the 2026-07-03 two-agents-one-identity bug.
    cfg = AgentConfig(
        name="parent-forked-triage",
        runtime="apptainer",
        env={TWIN_PARENT_ENV: "parent", TODO_AGENT_ENV: "parent"},
    )

    def _run() -> None:
        assert_twin_identity(cfg)

    # Act
    raised = pytest.raises(TwinIdentityError)
    # Assert
    with raised:
        _run()


def test_identity_gate_refuses_when_author_id_mismatches_own_name():
    # Arrange — one process, two names.
    cfg = AgentConfig(
        name="parent-forked-triage",
        runtime="apptainer",
        env={TWIN_PARENT_ENV: "parent", TODO_AGENT_ENV: "somebody-else"},
    )

    def _run() -> None:
        assert_twin_identity(cfg)

    # Act
    raised = pytest.raises(TwinIdentityError)
    # Assert
    with raised:
        _run()


