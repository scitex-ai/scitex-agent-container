from scitex_agent_container._lifecycle._start import _should_clear_persisted_session


def test_forced_process_restart_with_continue_preserves_conversation():
    # Arrange
    policy = "continue"
    # Act
    clear = _should_clear_persisted_session(
        force=True, explicit_session_override=policy
    )
    # Assert
    assert not clear


def test_only_forced_fresh_start_clears_conversation():
    # Arrange
    policy = "fresh"
    # Act
    clear = _should_clear_persisted_session(
        force=True, explicit_session_override=policy
    )
    # Assert
    assert clear


def test_fresh_policy_without_forced_replacement_does_not_clear_early():
    # Arrange
    policy = "fresh"
    # Act
    clear = _should_clear_persisted_session(
        force=False, explicit_session_override=policy
    )
    # Assert
    assert not clear


def test_spec_default_fresh_does_not_turn_force_into_implicit_erasure():
    # Arrange
    policy = None
    # Act
    clear = _should_clear_persisted_session(
        force=True, explicit_session_override=policy
    )
    # Assert
    assert not clear
