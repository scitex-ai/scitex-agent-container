"""Phase 1 import-surface lock for ``scitex_agent_container._telegram``.

Asserts only that the skeleton is importable and that public methods raise
``NotImplementedError``. Phase 2 will replace these with behaviour tests.
"""

from __future__ import annotations

import pytest


def test_telegram_package_exposes_bridge_class() -> None:
    # Arrange: import the package
    import scitex_agent_container._telegram as pkg

    # Act: read the public symbol
    cls = getattr(pkg, "TelegramBridge", None)

    # Assert: the class is exposed at package level
    assert cls is not None


def test_bridge_constructs_without_starting() -> None:
    # Arrange
    from scitex_agent_container._telegram import TelegramBridge

    # Act
    bridge = TelegramBridge(bot_token="dummy")

    # Assert: default mode is polling, not webhook
    assert bridge.webhook_mode is False


def test_connect_is_not_implemented_in_phase1() -> None:
    # Arrange
    from scitex_agent_container._telegram import TelegramBridge

    bridge = TelegramBridge(bot_token="dummy")
    raised: type[BaseException] | None = None

    # Act
    try:
        bridge.connect()
    except NotImplementedError as exc:  # noqa: BLE001
        raised = type(exc)

    # Assert
    assert raised is NotImplementedError


def test_disconnect_is_not_implemented_in_phase1() -> None:
    # Arrange
    from scitex_agent_container._telegram import TelegramBridge

    bridge = TelegramBridge(bot_token="dummy")
    raised: type[BaseException] | None = None

    # Act
    try:
        bridge.disconnect()
    except NotImplementedError as exc:  # noqa: BLE001
        raised = type(exc)

    # Assert
    assert raised is NotImplementedError


# Silence unused-import warning on `pytest` (kept for parity with the rest
# of the test suite which uses pytest fixtures elsewhere).
_ = pytest
