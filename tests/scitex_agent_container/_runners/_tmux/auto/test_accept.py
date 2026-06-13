"""Smoke surface for ``_runners/_tmux/auto/accept.py``.

The substantive auto-accept logic is dormant behind the TUI hedge
flag (``spec.runtime: tui``) and is exercised end-to-end by the
follow-up integration PR. Until then these tests pin the import
surface and the public callable shape so a regression that drops
``respond`` from the module surface fails CI loudly. One assert
per test, AAA markers each on their own line per STX-TQ002/007.
"""

from __future__ import annotations

from scitex_agent_container._runners._tmux.auto import accept as A


def test_respond_callable_exists_on_module_surface() -> None:
    # Arrange
    module = A
    # Act
    obj = getattr(module, "respond", None)
    # Assert
    assert callable(obj)


def test_yn_has_yes_option_returns_true_for_classic_one_yes_prompt() -> None:
    # Arrange
    pane = "Continue? [1] Yes [2] No"
    # Act
    matched = A._yn_has_yes_option(pane)
    # Assert
    assert matched is True


def test_yn_has_yes_option_returns_false_for_plain_text() -> None:
    # Arrange
    pane = "No prompt here, just narrative output."
    # Act
    matched = A._yn_has_yes_option(pane)
    # Assert
    assert matched is False
