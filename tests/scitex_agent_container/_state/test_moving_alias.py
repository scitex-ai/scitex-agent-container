"""A name that RESOLVES is not an identity that STAYS PUT.

INCIDENT 2026-08-05: three registries keyed a host on ``nas`` — a name the
operator's numbering scheme deliberately re-points as hardware is replaced
(``nas-01`` → ``nas-02`` → ``nas-03`` → …). One of them decided where the
production OAuth credential is pushed every four hours. Nothing was broken and
nothing would error: ``nas`` resolves correctly every time, right up to the day
it resolves to a different machine.

These tests pin the two properties that make the registry usable rather than
merely present: the moving name is refused, and the PINNED name it points to is
NOT — a guard that also rejects the replacement is a guard nobody can obey.
"""

from __future__ import annotations

from scitex_agent_container._state.moving_alias import (
    MOVING_ALIASES,
    MovingAliasError,
    moving_alias_hint,
    stable_name_for,
)


def test_bare_nas_maps_to_the_pinned_generation() -> None:
    """The operator's ruling: nas-03 is correct, nas is the moving alias."""
    # Arrange
    alias = "nas"
    # Act
    stable = stable_name_for(alias)
    # Assert
    assert stable == "nas-03"


def test_the_pinned_name_is_not_itself_refused() -> None:
    """nas-03 must pass, or the hint tells you to use a rejected name."""
    # Arrange
    replacement = "nas-03"
    # Act
    stable = stable_name_for(replacement)
    # Assert
    assert stable is None


def test_an_ordinary_peer_is_untouched() -> None:
    """Only registered movers are refused — not every unrecognised name."""
    # Arrange
    peer = "mba"
    # Act
    hint = moving_alias_hint(peer)
    # Assert
    assert hint is None


def test_the_hint_names_the_replacement() -> None:
    """An error that only states what broke is half-written."""
    # Arrange
    alias = "nas"
    # Act
    hint = moving_alias_hint(alias) or ""
    # Assert
    assert "nas-03" in hint


def test_the_hint_names_where_the_registry_lives() -> None:
    """The next generation lands eventually; say where to record it."""
    # Arrange
    alias = "nas"
    # Act
    hint = moving_alias_hint(alias) or ""
    # Assert
    assert "moving_alias.py" in hint


def test_the_hint_carries_the_caller_context() -> None:
    """The same registry serves config keys and dispatch targets."""
    # Arrange
    alias = "nas"
    # Act
    hint = moving_alias_hint(alias, context="dispatch target") or ""
    # Assert
    assert "as a dispatch target" in hint


def test_the_error_is_catchable_as_keyerror() -> None:
    """Existing `except KeyError` around peer lookup must keep working."""
    # Arrange
    raised = MovingAliasError("use nas-03")
    # Act
    caught = isinstance(raised, KeyError)
    # Assert
    assert caught is True


def test_the_error_message_is_not_repr_wrapped() -> None:
    """Bare KeyError repr's its arg, which would quote-wrap the whole hint."""
    # Arrange
    err = MovingAliasError("use nas-03")
    # Act
    rendered = str(err)
    # Assert
    assert rendered == "use nas-03"


def test_every_registered_alias_points_at_a_stable_name() -> None:
    """A mover whose replacement is itself a mover is an infinite loop."""
    # Arrange
    registry = dict(MOVING_ALIASES)
    # Act
    circular = [a for a, stable in registry.items() if stable in registry]
    # Assert
    assert circular == []
