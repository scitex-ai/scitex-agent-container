"""NoHealthyAccountError carries a one-line remedy beside its full diagnosis."""

from __future__ import annotations

import pytest

from scitex_agent_container._creds import NoHealthyAccountError
from scitex_agent_container._creds._pick_healthy import pick_healthy_account


def test_brief_defaults_to_the_full_message_when_not_supplied():
    # Arrange
    exc = NoHealthyAccountError("something went wrong")
    # Act
    actual = exc.brief
    # Assert
    assert actual == "something went wrong"


def test_brief_is_independent_of_the_full_message():
    # Arrange
    exc = NoHealthyAccountError("a long paragraph of reasoning", brief="short")
    # Act
    actual = exc.brief
    # Assert
    assert actual == "short"


def test_full_message_survives_alongside_the_brief():
    # Arrange
    exc = NoHealthyAccountError("a long paragraph of reasoning", brief="short")
    # Act
    actual = str(exc)
    # Assert
    assert actual == "a long paragraph of reasoning"


@pytest.fixture
def empty_store_brief(tmp_path) -> str:
    store = tmp_path / "accounts"
    store.mkdir()
    try:
        pick_healthy_account("", store_dir=store, home=tmp_path)
    except NoHealthyAccountError as exc:
        return exc.brief
    raise AssertionError("an empty account store must refuse")


def test_empty_store_brief_names_the_fixing_command(empty_store_brief):
    # Arrange
    expected = "sac accounts sync-live"
    # Act
    actual = empty_store_brief
    # Assert
    assert expected in actual


def test_empty_store_brief_stays_one_line(empty_store_brief):
    # Arrange
    forbidden = "\n"
    # Act
    actual = empty_store_brief
    # Assert
    assert forbidden not in actual
