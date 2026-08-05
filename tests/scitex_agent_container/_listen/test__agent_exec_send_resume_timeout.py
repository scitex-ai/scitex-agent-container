"""The ``claude --resume`` fallback must be BOUNDED, and say so when it fires.

Why this exists: the buffered fallback in
:func:`scitex_agent_container._listen._agent_exec_send.agent_send` ran
``subprocess.run(...)`` with no ``timeout=`` at all. A re-launch that
never returned held the request open forever; every caller absorbed the
wait privately and then blamed its own client deadline on a `sac listen`
outage. The daemon looked healthy throughout because, on every other
route, it was — which is what made the misdiagnosis so persuasive
(card sac-listen-send-endpoint-wedged-fleet-wide-20260803).

An unbounded wait on a subprocess is not patience. It is a hang with no
upper bound and no signal.

The bound is deliberately generous (a real turn takes minutes); the point
is that one EXISTS and is overridable per host, not that it is tight.

NO MOCKS — these drive the real ``_resume_timeout_s`` against real
environment variables via the package ``env_save_restore`` fixture.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._listen._agent_exec_send import (
    DEFAULT_RESUME_TIMEOUT_S,
    _resume_timeout_s,
)


def test_unset_env_uses_the_default_bound(env_save_restore) -> None:
    """No override → the documented default, not "no timeout"."""
    # Arrange
    env_save_restore.set("SAC_LISTEN_RESUME_TIMEOUT_S", "")
    # Act
    value = _resume_timeout_s()
    # Assert
    assert value == DEFAULT_RESUME_TIMEOUT_S


def test_default_bound_is_finite() -> None:
    """The whole point: there IS an upper bound.

    A regression that restored ``None``/``inf`` here would reinstate the
    original defect while every other test still passed.
    """
    # Arrange
    # Act
    value = DEFAULT_RESUME_TIMEOUT_S
    # Assert
    assert 0 < value < float("inf")


def test_env_override_is_honoured(env_save_restore) -> None:
    """An operator who sets the bound gets the bound they set."""
    # Arrange
    env_save_restore.set("SAC_LISTEN_RESUME_TIMEOUT_S", "42")
    # Act
    value = _resume_timeout_s()
    # Assert
    assert value == 42.0


def test_malformed_env_raises_rather_than_silently_defaulting(
    env_save_restore,
) -> None:
    """``"30s"`` is a stated intent; ignoring it would be a silent fallback.

    Reverting to the default here would leave the operator believing a
    30-second bound was in force while a 300-second one actually was —
    the "looks configured, isn't" shape this change exists to remove.
    """
    # Arrange
    env_save_restore.set("SAC_LISTEN_RESUME_TIMEOUT_S", "30s")
    # Act
    # Assert
    with pytest.raises(ValueError):
        _resume_timeout_s()


def test_malformed_env_error_names_the_variable(env_save_restore) -> None:
    """Actionable: the message must say WHICH knob is wrong.

    An error that only says "invalid timeout" leaves the reader grepping
    for which of several env vars they fat-fingered.
    """
    # Arrange
    env_save_restore.set("SAC_LISTEN_RESUME_TIMEOUT_S", "abc")
    # Act — capture without pytest.raises so this stays a single assertion.
    try:
        _resume_timeout_s()
        message = ""
    except ValueError as exc:
        message = str(exc)
    # Assert
    assert "SAC_LISTEN_RESUME_TIMEOUT_S" in message


def test_zero_bound_is_rejected(env_save_restore) -> None:
    """A zero bound would kill every re-launch instantly — refuse it."""
    # Arrange
    env_save_restore.set("SAC_LISTEN_RESUME_TIMEOUT_S", "0")
    # Act
    # Assert
    with pytest.raises(ValueError):
        _resume_timeout_s()


def test_negative_bound_is_rejected(env_save_restore) -> None:
    """Same for a negative one; nonsense must not become policy."""
    # Arrange
    env_save_restore.set("SAC_LISTEN_RESUME_TIMEOUT_S", "-5")
    # Act
    # Assert
    with pytest.raises(ValueError):
        _resume_timeout_s()
