"""The blind-cache refusal must be discriminable BY TYPE, not by message text.

Operator 2026-08-02: 「refresh quota cache 勝手にやれよ」 — a blind cache had
just refused three of five agents in one ``sac-restart``, telling the operator
to run the refresh by hand. The caller now refreshes and re-picks itself.

That retry is only safe if it fires for the ONE repairable failure and no
other: every other ``NoHealthyAccountError`` (no fresh candidate, all accounts
expired) needs a human to log in, and retrying those would loop. These tests
pin the type relationship the retry depends on — matching on message text
would have made the retry silently over- or under-fire the first time anyone
reworded the error.

PA-307 / STX-TQ002 / STX-TQ007 — one assert per test, full AAA markers.
"""

from __future__ import annotations

from scitex_agent_container._creds import (
    BlindQuotaCacheError,
    NoHealthyAccountError,
)


def test_blind_error_is_catchable_as_the_family() -> None:
    # Arrange — existing callers catch the parent and must keep working.
    err = BlindQuotaCacheError("blind")
    # Act
    caught = isinstance(err, NoHealthyAccountError)
    # Assert
    assert caught


def test_family_error_is_not_mistaken_for_the_blind_case() -> None:
    # Arrange — an "all accounts expired" failure. Refreshing the quota cache
    # CANNOT repair this, so the retry must not fire for it.
    err = NoHealthyAccountError("every stored account is expired")
    # Act
    would_retry = isinstance(err, BlindQuotaCacheError)
    # Assert
    assert not would_retry


def test_blind_error_is_exported_from_the_package_surface() -> None:
    # Arrange — the caller imports it from ``.._creds``, not the private
    # module, so the export is part of the contract the retry relies on.
    import scitex_agent_container._creds as creds

    # Act
    exported = "BlindQuotaCacheError" in creds.__all__
    # Assert
    assert exported
