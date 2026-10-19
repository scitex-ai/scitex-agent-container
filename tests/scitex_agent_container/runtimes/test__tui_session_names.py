"""``_tui_session_names`` — bare canonical sessions, legacy ``tui-`` recognised.

New sessions are minted bare (the session name IS the agent name);
``tui-<agent>`` sessions from before the cutover keep resolving. Every
lookup tries the canonical name first and the legacy name second.

STX-TQ002 AAA-marker + STX-TQ007 one-assert. No mocks.
"""

from __future__ import annotations

from scitex_agent_container.runtimes._tui_session_names import (
    agent_of_session,
    candidate_session_names,
    canonical_session_name,
    legacy_session_name,
    resolve_against_snapshot,
    resolve_live_session,
    session_aliases,
    session_and_aliases,
    session_present,
)


def test_canonical_session_name_is_the_bare_agent_name() -> None:
    # Arrange
    agent = "alpha"
    # Act
    name = canonical_session_name(agent)
    # Assert
    assert name == "alpha"


def test_legacy_session_name_carries_the_prefix() -> None:
    # Arrange
    agent = "alpha"
    # Act
    name = legacy_session_name(agent)
    # Assert
    assert name == "tui-alpha"


def test_candidates_prefer_bare_then_legacy() -> None:
    # Arrange
    agent = "alpha"
    # Act
    names = candidate_session_names(agent)
    # Assert
    assert names == ("alpha", "tui-alpha")


def test_resolve_live_session_prefers_bare() -> None:
    # Arrange — a cutover-straddling host carries BOTH names.
    sessions = ["tui-alpha", "alpha", "other"]
    # Act
    live = resolve_live_session("alpha", sessions)
    # Assert
    assert live == "alpha"


def test_resolve_live_session_falls_back_to_legacy() -> None:
    # Arrange — only the pre-cutover session is live.
    sessions = ["tui-alpha", "other"]
    # Act
    live = resolve_live_session("alpha", sessions)
    # Assert
    assert live == "tui-alpha"


def test_resolve_live_session_defaults_to_bare_when_absent() -> None:
    # Arrange — neither candidate is live: default to what minting needs.
    sessions = ["other"]
    # Act
    live = resolve_live_session("alpha", sessions)
    # Assert
    assert live == "alpha"


def test_resolve_live_session_defaults_to_bare_when_blind() -> None:
    # Arrange — the listing could not be read at all.
    # Act
    live = resolve_live_session("alpha", None)
    # Assert
    assert live == "alpha"


def test_session_present_true_for_legacy_only() -> None:
    # Arrange — only the pre-cutover session is live.
    sessions = ["tui-alpha"]
    # Act
    present = session_present("alpha", sessions)
    # Assert
    assert present is True


def test_session_present_false_when_neither_candidate_live() -> None:
    # Arrange — the instrument looked and the agent is in neither slot.
    sessions = ["other"]
    # Act
    present = session_present("alpha", sessions)
    # Assert
    assert present is False


def test_session_present_none_when_blind() -> None:
    # Arrange — no listing: absence of evidence, never evidence of absence.
    # Act
    present = session_present("alpha", None)
    # Assert
    assert present is None


def test_agent_of_session_strips_one_legacy_level() -> None:
    # Arrange
    session = "tui-alpha"
    # Act
    agent = agent_of_session(session)
    # Assert
    assert agent == "alpha"


def test_agent_of_session_leaves_bare_unchanged() -> None:
    # Arrange
    session = "alpha"
    # Act
    agent = agent_of_session(session)
    # Assert
    assert agent == "alpha"


def test_session_aliases_name_the_legacy_form() -> None:
    # Arrange
    session = "alpha"
    # Act
    aliases = session_aliases(session)
    # Assert
    assert aliases == ("tui-alpha",)


def test_session_aliases_empty_for_already_legacy() -> None:
    # Arrange
    session = "tui-alpha"
    # Act
    aliases = session_aliases(session)
    # Assert
    assert aliases == ()


def test_session_and_aliases_prefers_canonical() -> None:
    # Arrange
    session = "alpha"
    # Act
    names = session_and_aliases(session)
    # Assert
    assert names == ("alpha", "tui-alpha")


def test_resolve_against_snapshot_finds_legacy() -> None:
    # Arrange — the snapshot keys a pre-cutover session.
    snapshot = {"tui-alpha": 1}
    # Act
    present = resolve_against_snapshot("alpha", snapshot)
    # Assert
    assert present is True


def test_resolve_against_snapshot_none_when_blind() -> None:
    # Arrange
    snapshot = None
    # Act
    present = resolve_against_snapshot("alpha", snapshot)
    # Assert
    assert present is None
