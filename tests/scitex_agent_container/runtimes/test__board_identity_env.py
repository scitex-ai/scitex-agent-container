"""Board-identity env injection + the unexpanded-``${VAR}`` validator.

Mirrors ``src/scitex_agent_container/runtimes/_board_identity_env.py`` (PS-204 §2).

INCIDENT 2026-07-19: scitex-cards renamed ``SCITEX_TODO_AGENT_ID`` ->
``SCITEX_CARDS_AGENT_ID``; sac injected only the OLD name, so a ``.mcp.json``
referencing the new one expanded to nothing and seven live cards recorded
``created_by = '${SCITEX_CARDS_AGENT_ID}'`` — the variable NAME stored as data.

Two properties are load-bearing and each is asserted in BOTH directions:

* BOTH names reach the container with the SAME value (not a swap — the fleet's
  installed scitex-cards versions differ, so dropping either name breaks the
  half of the fleet that reads it).
* A value that still looks like an unexpanded substitution is REJECTED, while
  an ordinary value passes through untouched. Without the second direction the
  validator could "pass" by rejecting everything.

Plain dicts and real ``SimpleNamespace`` configs — no mocks, no monkeypatch
(PA-306). Every seam here already takes its inputs as parameters.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

from scitex_agent_container.runtimes._board_identity_env import (
    BOARD_ID_ENV,
    LEGACY_BOARD_ID_ENV,
    UnexpandedEnvValueError,
    apply_board_identity_alias,
    assert_expanded,
    raw_args_env,
    reject_unexpanded_env,
)
from scitex_agent_container.runtimes._fleet_env import fleet_env_flags


def _raised_by(call: Callable[[], Any]) -> BaseException | None:
    """Return the exception ``call`` raised, or ``None`` if it returned.

    Lets a "this must be rejected" test assert ONCE (on the captured
    exception) instead of pairing a ``pytest.raises`` block with a second
    assertion about the message — which STX-TQ007 counts as two.
    """
    try:
        call()
    except BaseException as exc:  # noqa: BLE001 - the captured value IS the result
        return exc
    return None


# ----------------------------------------------------------------------
# The validator — what it rejects.
# ----------------------------------------------------------------------


def test_a_whole_value_substitution_ref_is_rejected() -> None:
    # Arrange
    value = "${SCITEX_CARDS_AGENT_ID}"
    # Act
    raised = _raised_by(lambda: assert_expanded(BOARD_ID_ENV, value))
    # Assert
    assert isinstance(raised, UnexpandedEnvValueError)


def test_an_embedded_substitution_ref_is_also_rejected() -> None:
    """``agent-${VAR}`` is just as unexpanded, and just as unusable as data."""
    # Arrange
    value = "agent-${SUFFIX}"
    # Act
    raised = _raised_by(lambda: assert_expanded("SOME_KEY", value))
    # Assert
    assert isinstance(raised, UnexpandedEnvValueError)


def test_the_rejection_is_a_value_error_subclass() -> None:
    """Callers already catching ValueError must still catch this."""
    # Arrange
    value = "${V}"
    # Act
    raised = _raised_by(lambda: assert_expanded("K", value))
    # Assert
    assert isinstance(raised, ValueError)


def test_the_error_message_names_the_offending_variable() -> None:
    # Arrange
    name = "SCITEX_CARDS_AGENT_ID"
    # Act
    raised = _raised_by(lambda: assert_expanded(name, "${SCITEX_CARDS_AGENT_ID}"))
    # Assert
    assert name in str(raised)


def test_the_error_message_quotes_the_offending_value() -> None:
    # Arrange
    value = "${SOMETHING}"
    # Act
    raised = _raised_by(lambda: assert_expanded("K", value))
    # Assert
    assert value in str(raised)


def test_the_error_message_names_the_likely_cause() -> None:
    """A renamed variable that nobody exports is the cause worth naming."""
    # Arrange
    value = "${V}"
    # Act
    raised = _raised_by(lambda: assert_expanded("K", value))
    # Assert
    assert "RENAMED" in str(raised)


# ----------------------------------------------------------------------
# The validator — what it must NOT reject (the controls).
# ----------------------------------------------------------------------


def test_an_ordinary_value_is_accepted_unchanged() -> None:
    # Arrange
    env = {BOARD_ID_ENV: "scitex-agent-container"}
    # Act
    out = reject_unexpanded_env(env)
    # Assert
    assert out[BOARD_ID_ENV] == "scitex-agent-container"


def test_a_bare_dollar_sign_value_is_accepted() -> None:
    """Only the ``${`` substitution SHAPE is a non-answer; ``$`` alone is data."""
    # Arrange
    env = {"PRICE": "$5 and $HOME-ish"}
    # Act
    out = reject_unexpanded_env(env)
    # Assert
    assert out["PRICE"] == "$5 and $HOME-ish"


def test_an_empty_value_is_accepted() -> None:
    """The documented per-agent fleet-default opt-out sets an empty value."""
    # Arrange
    env = {"SHARED_KEY": ""}
    # Act
    out = reject_unexpanded_env(env)
    # Assert
    assert out["SHARED_KEY"] == ""


def test_validation_does_not_mutate_the_input_mapping() -> None:
    # Arrange
    env = {"K": "v"}
    # Act
    reject_unexpanded_env(env)["K"] = "MUTATED"
    # Assert
    assert env["K"] == "v"


# ----------------------------------------------------------------------
# raw_args parsing — both spellings occur in real specs.
# ----------------------------------------------------------------------


def test_the_split_env_spelling_is_parsed() -> None:
    """``["--env", "K=V"]`` — two argv elements (most specs)."""
    # Arrange
    raw_args = ["--userns", "--env", "SCITEX_TODO_AGENT_ID=scitex-dev"]
    # Act
    found = raw_args_env(raw_args)
    # Assert
    assert found["SCITEX_TODO_AGENT_ID"] == "scitex-dev"


def test_the_glued_env_spelling_is_parsed() -> None:
    """``["--env=K=V"]`` — one argv element (scitex-agentic-journal et al)."""
    # Arrange
    raw_args = ["--containall", "--env=SCITEX_TODO_AGENT_ID=agentic-journal"]
    # Act
    found = raw_args_env(raw_args)
    # Assert
    assert found["SCITEX_TODO_AGENT_ID"] == "agentic-journal"


def test_a_later_duplicate_env_flag_wins() -> None:
    """Matches apptainer's own ``--env`` last-wins precedence."""
    # Arrange
    raw_args = ["--env", "K=first", "--env", "K=second"]
    # Act
    found = raw_args_env(raw_args)
    # Assert
    assert found["K"] == "second"


def test_a_value_containing_equals_survives_intact() -> None:
    # Arrange
    raw_args = ["--env", "GIT_SSH_COMMAND=ssh -o ControlPath=none"]
    # Act
    found = raw_args_env(raw_args)
    # Assert
    assert found["GIT_SSH_COMMAND"] == "ssh -o ControlPath=none"


def test_a_trailing_env_flag_is_skipped_not_fatal() -> None:
    """sac must not refuse to launch over an operator escape hatch it misreads."""
    # Arrange
    raw_args = ["--userns", "--env"]
    # Act
    found = raw_args_env(raw_args)
    # Assert
    assert found == {}


def test_non_env_raw_args_contribute_nothing() -> None:
    # Arrange
    raw_args = ["--home", "/home/agent", "--overlay", "/some/path/"]
    # Act
    found = raw_args_env(raw_args)
    # Assert
    assert found == {}


def test_absent_raw_args_yield_an_empty_mapping() -> None:
    # Arrange
    raw_args = None
    # Act
    found = raw_args_env(raw_args)
    # Assert
    assert found == {}


# ----------------------------------------------------------------------
# The alias — BOTH names, same value.
# ----------------------------------------------------------------------


def test_the_legacy_name_is_mirrored_onto_the_current_name() -> None:
    # Arrange
    env = {LEGACY_BOARD_ID_ENV: "scitex-agent-container"}
    # Act
    out = apply_board_identity_alias(env)
    # Assert
    assert out[BOARD_ID_ENV] == "scitex-agent-container"


def test_the_current_name_is_mirrored_onto_the_legacy_name() -> None:
    """A spec that already migrated must not strand pre-rename scitex-cards."""
    # Arrange
    env = {BOARD_ID_ENV: "scitex-agent-container"}
    # Act
    out = apply_board_identity_alias(env)
    # Assert
    assert out[LEGACY_BOARD_ID_ENV] == "scitex-agent-container"


def test_both_names_carry_exactly_the_same_value() -> None:
    # Arrange
    env = {LEGACY_BOARD_ID_ENV: "worker-telegrammer-orochi"}
    # Act
    out = apply_board_identity_alias(env)
    # Assert
    assert out[BOARD_ID_ENV] == out[LEGACY_BOARD_ID_ENV]


def test_an_explicit_current_name_is_not_clobbered() -> None:
    """The mirror FILLS IN an absent name; it never overwrites a declared one."""
    # Arrange
    env = {LEGACY_BOARD_ID_ENV: "old-value", BOARD_ID_ENV: "explicit-value"}
    # Act
    out = apply_board_identity_alias(env)
    # Assert
    assert out[BOARD_ID_ENV] == "explicit-value"


def test_a_raw_args_identity_is_mirrored_when_spec_env_is_silent() -> None:
    """Most of the fleet declares the identity ONLY in raw_args."""
    # Arrange
    raw_args = ["--env", f"{LEGACY_BOARD_ID_ENV}=scitex-dev"]
    # Act
    out = apply_board_identity_alias({}, raw_args=raw_args)
    # Assert
    assert out[BOARD_ID_ENV] == "scitex-dev"


def test_a_raw_args_identity_overrides_the_spec_env_identity() -> None:
    """raw_args are appended AFTER spec.env flags and apptainer --env is last-wins."""
    # Arrange
    raw_args = ["--env", f"{LEGACY_BOARD_ID_ENV}=from-raw-args"]
    # Act
    out = apply_board_identity_alias(
        {LEGACY_BOARD_ID_ENV: "from-spec-env"}, raw_args=raw_args
    )
    # Assert
    assert out[BOARD_ID_ENV] == "from-raw-args"


def test_the_legacy_name_is_left_to_raw_args_that_declare_it() -> None:
    """Re-emitting it from spec.env would be shadowed anyway — emit nothing."""
    # Arrange
    raw_args = ["--env", f"{LEGACY_BOARD_ID_ENV}=from-raw-args"]
    # Act
    out = apply_board_identity_alias({}, raw_args=raw_args)
    # Assert
    assert LEGACY_BOARD_ID_ENV not in out


def test_an_unexpanded_raw_args_identity_is_rejected() -> None:
    """The corruption's exact shape, arriving through the escape hatch."""
    # Arrange
    raw_args = ["--env", f"{BOARD_ID_ENV}=${{{BOARD_ID_ENV}}}"]
    # Act
    raised = _raised_by(lambda: apply_board_identity_alias({}, raw_args=raw_args))
    # Assert
    assert isinstance(raised, UnexpandedEnvValueError)


def test_an_agent_with_no_identity_gets_no_invented_one() -> None:
    """No identity anywhere means sac must not guess — never a silent default."""
    # Arrange
    env = {"UNRELATED": "value"}
    # Act
    out = apply_board_identity_alias(env)
    # Assert
    assert BOARD_ID_ENV not in out


def test_a_blank_identity_is_not_treated_as_an_identity() -> None:
    # Arrange
    env = {LEGACY_BOARD_ID_ENV: "   "}
    # Act
    out = apply_board_identity_alias(env)
    # Assert
    assert BOARD_ID_ENV not in out


def test_unrelated_env_pairs_survive_the_alias_step() -> None:
    # Arrange
    env = {LEGACY_BOARD_ID_ENV: "agent", "SCITEX_CARDS_DUAL_WRITE": "1"}
    # Act
    out = apply_board_identity_alias(env)
    # Assert
    assert out["SCITEX_CARDS_DUAL_WRITE"] == "1"


def test_the_alias_does_not_mutate_the_input_mapping() -> None:
    # Arrange
    env = {LEGACY_BOARD_ID_ENV: "agent"}
    # Act
    apply_board_identity_alias(env)
    # Assert
    assert BOARD_ID_ENV not in env


def test_applying_the_alias_twice_is_idempotent() -> None:
    # Arrange
    once = apply_board_identity_alias({LEGACY_BOARD_ID_ENV: "agent"})
    # Act
    twice = apply_board_identity_alias(once)
    # Assert
    assert twice == once


# ----------------------------------------------------------------------
# The rendered argv — what actually reaches the container.
# ----------------------------------------------------------------------


def test_the_current_name_reaches_the_rendered_env_flags() -> None:
    """A merge correct in a dict and wrong in the flags is still a broken fix."""
    # Arrange
    config = SimpleNamespace(env={LEGACY_BOARD_ID_ENV: "scitex-agent-container"})
    # Act
    flags = fleet_env_flags(config, defaults={})
    # Assert
    assert f"{BOARD_ID_ENV}=scitex-agent-container" in flags


def test_the_legacy_name_reaches_the_rendered_env_flags() -> None:
    # Arrange
    config = SimpleNamespace(env={LEGACY_BOARD_ID_ENV: "scitex-agent-container"})
    # Act
    flags = fleet_env_flags(config, defaults={})
    # Assert
    assert f"{LEGACY_BOARD_ID_ENV}=scitex-agent-container" in flags
