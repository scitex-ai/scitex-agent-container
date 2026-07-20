"""Route resolution — and the rule that an empty enumeration may not convict."""

from __future__ import annotations

from scitex_agent_container._delivery._route import (
    STRATEGY_SDK,
    STRATEGY_TUI,
    resolve_route,
)


class SessionLister:
    """A real ``list_sessions_fn() -> list[str] | None`` returning a fixed answer."""

    def __init__(self, sessions):
        self._sessions = sessions
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self._sessions


class SessionIdReader:
    """A real ``session_id_fn(agent) -> str | None`` returning a fixed answer."""

    def __init__(self, session_id):
        self._session_id = session_id

    def __call__(self, agent):
        return self._session_id


def test_present_session_resolves_the_route():
    # Arrange
    lister = SessionLister(["tui-peer", "tui-other"])
    # Act
    route = resolve_route("peer", strategy=STRATEGY_TUI, list_sessions_fn=lister)
    # Assert
    assert route.resolved is True


def test_absent_among_others_refutes_route():
    # Arrange
    lister = SessionLister(["tui-other", "tui-third"])
    # Act
    route = resolve_route("peer", strategy=STRATEGY_TUI, list_sessions_fn=lister)
    # Assert
    assert route.resolved is False


def test_empty_enumeration_refuses_to_convict():
    # Arrange
    lister = SessionLister([])
    # Act
    route = resolve_route("peer", strategy=STRATEGY_TUI, list_sessions_fn=lister)
    # Assert
    assert route.resolved is None


def test_failed_enumeration_renders_unknown_route():
    # Arrange
    lister = SessionLister(None)
    # Act
    route = resolve_route("peer", strategy=STRATEGY_TUI, list_sessions_fn=lister)
    # Assert
    assert route.resolved is None


def test_empty_enumeration_names_the_namespace_reason():
    # Arrange
    lister = SessionLister([])
    # Act
    route = resolve_route("peer", strategy=STRATEGY_TUI, list_sessions_fn=lister)
    # Assert
    assert "mount namespace" in route.reason


def test_recorded_session_id_selects_sdk():
    # Arrange
    reader = SessionIdReader("sid-abc123")
    # Act
    route = resolve_route("peer", strategy="auto", session_id_fn=reader)
    # Assert
    assert route.strategy == STRATEGY_SDK


def test_missing_session_id_falls_back_tui():
    # Arrange
    lister = SessionLister(["tui-peer"])
    # Act
    route = resolve_route(
        "peer",
        strategy="auto",
        list_sessions_fn=lister,
        session_id_fn=SessionIdReader(None),
    )
    # Assert
    assert route.strategy == STRATEGY_TUI


def test_auto_never_enumerates_when_sdk_resolves():
    # Arrange
    lister = SessionLister(["tui-peer"])
    # Act
    resolve_route(
        "peer",
        strategy="auto",
        list_sessions_fn=lister,
        session_id_fn=SessionIdReader("sid-abc123"),
    )
    # Assert
    assert lister.calls == 0


def test_forced_sdk_without_session_refutes():
    # Arrange
    reader = SessionIdReader(None)
    # Act
    route = resolve_route("peer", strategy=STRATEGY_SDK, session_id_fn=reader)
    # Assert
    assert route.resolved is False
