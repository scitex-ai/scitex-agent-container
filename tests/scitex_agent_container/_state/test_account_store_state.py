"""Tests for the three-valued account-store classification.

No mocks: every case builds a REAL directory on tmp_path (or points at a
path that genuinely does not exist) and reads it back, because the whole
defect being fixed is a wrong answer about the filesystem — a stubbed
filesystem could not have caught it and cannot guard it.

The load-bearing tests are the two that assert an ABSENT store is NOT
reported as an empty one. That collapse is what told an agent the fleet
controller had zero accounts while four were healthy on the host.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state.account_store_state import (
    ABSENT,
    READABLE,
    UNREADABLE,
    StoreState,
    classify_store,
    no_accounts_message,
)


def _make_store(root: Path, *account_names: str) -> Path:
    store = root / ".scitex" / "agent-container" / "accounts"
    store.mkdir(parents=True)
    for name in account_names:
        acct = store / name
        acct.mkdir()
        (acct / "account.json").write_text('{"name": "%s"}' % name)
    return store


def test_a_missing_store_is_absent_not_empty(tmp_path: Path) -> None:
    """The defect this module exists to prevent."""
    # Arrange — a home with no store in it at all.
    home = tmp_path / "nowhere"
    home.mkdir()
    # Act
    state = classify_store(home=home)
    # Assert
    assert state.state == ABSENT


def test_a_missing_store_reports_no_count(tmp_path: Path) -> None:
    """A count nobody could take must not be expressible as a number."""
    # Arrange
    home = tmp_path / "nowhere2"
    home.mkdir()
    # Act
    state = classify_store(home=home)
    # Assert
    assert state.account_count is None


def test_an_empty_store_is_readable_with_zero(tmp_path: Path) -> None:
    """The OTHER empty — genuinely no accounts — still reports zero."""
    # Arrange
    _make_store(tmp_path)
    # Act
    state = classify_store(home=tmp_path)
    # Assert
    assert state.state == READABLE


def test_an_empty_store_counts_zero(tmp_path: Path) -> None:
    # Arrange
    _make_store(tmp_path)
    # Act
    state = classify_store(home=tmp_path)
    # Assert
    assert state.account_count == 0


def test_a_populated_store_counts_its_accounts(tmp_path: Path) -> None:
    # Arrange
    _make_store(tmp_path, "alice-example-com", "bob-example-com")
    # Act
    state = classify_store(home=tmp_path)
    # Assert
    assert state.account_count == 2


def test_a_store_path_that_is_a_file_is_unreadable(tmp_path: Path) -> None:
    # Arrange — the path exists but is not a directory.
    store = tmp_path / ".scitex" / "agent-container"
    store.mkdir(parents=True)
    (store / "accounts").write_text("not a directory")
    # Act
    state = classify_store(home=tmp_path)
    # Assert
    assert state.state == UNREADABLE


def test_empty_is_trustworthy_only_when_readable(tmp_path: Path) -> None:
    # Arrange
    home = tmp_path / "gone"
    home.mkdir()
    # Act
    state = classify_store(home=home)
    # Assert
    assert state.empty_is_trustworthy is False


def test_the_resolved_path_is_reported_even_on_failure(tmp_path: Path) -> None:
    """Which path did you look at — the first question a wrong answer raises."""
    # Arrange
    home = tmp_path / "missing"
    home.mkdir()
    # Act
    state = classify_store(home=home)
    # Assert
    assert str(state.path).startswith(str(home))


def test_absent_message_refuses_to_advise_creating_an_account(tmp_path: Path) -> None:
    """Telling an operator to `account save` here is the actual harm."""
    # Arrange
    home = tmp_path / "unbound"
    home.mkdir()
    state = classify_store(home=home)
    # Act
    message = no_accounts_message(state)
    # Assert
    assert "account save" not in message


def test_absent_message_names_the_path_it_looked_at(tmp_path: Path) -> None:
    # Arrange
    home = tmp_path / "unbound2"
    home.mkdir()
    state = classify_store(home=home)
    # Act
    message = no_accounts_message(state)
    # Assert
    assert str(state.path) in message


def test_readable_empty_message_does_advise_creating_an_account(tmp_path: Path) -> None:
    """The genuine empty keeps the helpful advice — that case was never wrong.

    ``homes_root`` points at an EMPTY directory so this is a real "no store
    anywhere else" machine. Without that the first version of this test read
    the live host, found the operator's populated store, and got the shadow
    message — a test that reported the machine instead of the behaviour.
    """
    # Arrange
    _make_store(tmp_path)
    state = classify_store(home=tmp_path)
    empty_homes = tmp_path / "no-other-homes"
    empty_homes.mkdir()
    # Act
    message = no_accounts_message(state, homes_root=empty_homes)
    # Assert
    assert "account save" in message


def test_a_populated_neighbour_store_is_reported_as_a_shadow(tmp_path: Path) -> None:
    """The defect measured 2026-08-17: empty here, four accounts next door."""
    # Arrange — this home is empty; a neighbour home holds an account.
    homes = tmp_path / "homes"
    mine = homes / "agent"
    mine.mkdir(parents=True)
    _make_store(mine)
    _make_store(homes / "operator", "scitex-01-scitex-ai")
    state = classify_store(home=mine)
    # Act
    message = no_accounts_message(state, homes_root=homes)
    # Assert
    assert "empty shadow" in message


def test_the_shadow_message_refuses_to_advise_creating_an_account(
    tmp_path: Path,
) -> None:
    """Creating an account here would add a fifth to a store nobody reads."""
    # Arrange
    homes = tmp_path / "homes2"
    mine = homes / "agent"
    mine.mkdir(parents=True)
    _make_store(mine)
    _make_store(homes / "operator", "scitex-01-scitex-ai")
    state = classify_store(home=mine)
    # Act
    message = no_accounts_message(state, homes_root=homes)
    # Assert
    assert "Do NOT create a new account" in message


def test_the_shadow_message_names_the_populated_store(tmp_path: Path) -> None:
    """Naming WHERE the accounts are is what makes this actionable."""
    # Arrange
    homes = tmp_path / "homes3"
    mine = homes / "agent"
    mine.mkdir(parents=True)
    _make_store(mine)
    real = _make_store(homes / "operator", "scitex-01-scitex-ai")
    state = classify_store(home=mine)
    # Act
    message = no_accounts_message(state, homes_root=homes)
    # Assert
    assert str(real) in message


def test_a_readable_state_must_carry_a_count() -> None:
    """The validator fails where the value is built, not three layers down."""
    # Arrange
    build = lambda: StoreState(path=Path("/x"), state=READABLE, account_count=None)
    # Act
    raised = pytest.raises(ValueError)
    # Assert
    with raised:
        build()


def test_an_absent_state_must_not_carry_a_count() -> None:
    """Constructing the collapse itself is an error."""
    # Arrange
    build = lambda: StoreState(path=Path("/x"), state=ABSENT, account_count=0)
    # Act
    raised = pytest.raises(ValueError)
    # Assert
    with raised:
        build()


def test_an_unknown_state_string_is_rejected() -> None:
    # Arrange
    build = lambda: StoreState(path=Path("/x"), state="probably-fine", account_count=None)
    # Act
    raised = pytest.raises(ValueError)
    # Assert
    with raised:
        build()
