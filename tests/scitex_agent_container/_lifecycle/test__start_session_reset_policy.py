from scitex_agent_container._lifecycle._start import _should_clear_persisted_session


def test_forced_process_restart_with_continue_preserves_conversation():
    assert not _should_clear_persisted_session(
        force=True, explicit_session_override="continue"
    )


def test_only_forced_fresh_start_clears_conversation():
    assert _should_clear_persisted_session(
        force=True, explicit_session_override="fresh"
    )


def test_fresh_policy_without_forced_replacement_does_not_clear_early():
    assert not _should_clear_persisted_session(
        force=False, explicit_session_override="fresh"
    )


def test_spec_default_fresh_does_not_turn_force_into_implicit_erasure():
    assert not _should_clear_persisted_session(
        force=True, explicit_session_override=None
    )
