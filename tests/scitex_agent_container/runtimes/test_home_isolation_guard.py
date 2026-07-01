"""Guard: refuse specs whose $HOME is served by an operator-writable bind."""

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ApptainerSpec
from scitex_agent_container.runtimes._home_isolation_guard import (
    assert_home_not_operator_writable,
)


def _config(*, binds=None, raw_args=None):
    return AgentConfig(
        name="test-agent",
        apptainer=ApptainerSpec(binds=binds or [], raw_args=raw_args or []),
    )


def test_canonical_home_with_whole_home_rw_bind_passes():
    # Arrange: $HOME=/home/agent (default), not under the rw /home/ywatanabe bind.
    config = _config(binds=["/home/ywatanabe:/home/ywatanabe:rw"])
    # Act
    result = assert_home_not_operator_writable(config)
    # Assert
    assert result is None


def test_ro_identity_bind_under_home_passes():
    # Arrange: a read-only mount into the agent home cannot clobber anything.
    config = _config(binds=["/home/ywatanabe/.ssh:/home/agent/.ssh:ro"])
    # Act
    result = assert_home_not_operator_writable(config)
    # Assert
    assert result is None


def test_home_moved_onto_rw_bind_raises():
    # Arrange
    config = _config(
        binds=["/home/ywatanabe:/home/ywatanabe:rw"],
        raw_args=["--home", "/home/ywatanabe"],
    )
    # Act
    guard = assert_home_not_operator_writable
    # Assert
    with pytest.raises(RuntimeError):
        guard(config)


def test_home_under_ancestor_rw_bind_raises():
    # Arrange
    config = _config(
        binds=["/home/ywatanabe:/home/ywatanabe:rw"],
        raw_args=["--home", "/home/ywatanabe/sub"],
    )
    # Act
    guard = assert_home_not_operator_writable
    # Assert
    with pytest.raises(RuntimeError):
        guard(config)


def test_rw_bind_via_raw_args_bind_flag_raises():
    # Arrange: bind declared through raw_args with no explicit mode defaults to rw.
    config = _config(
        raw_args=[
            "--home",
            "/home/ywatanabe",
            "--bind",
            "/home/ywatanabe:/home/ywatanabe",
        ],
    )
    # Act
    guard = assert_home_not_operator_writable
    # Assert
    with pytest.raises(RuntimeError):
        guard(config)
