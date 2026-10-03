"""Explicit session wire input becomes the canonical host CLI option."""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._start_session_wire import start_session_cli_args


@pytest.mark.parametrize("mode", ("", "typo", False, {}, []))
def test_invalid_session_wire_is_refused(mode):
    # Arrange
    selected = mode
    # Act
    # Assert
    with pytest.raises(ValueError, match="'session' must be"):
        start_session_cli_args(selected)
