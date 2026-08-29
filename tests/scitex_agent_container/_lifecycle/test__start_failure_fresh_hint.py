"""A failed start must name the ONE-SHOT ``--fresh`` escape hatch — and only there.

WHY THIS FILE EXISTS (operator, 2026-08-18). 「フレッシュは基本的に使いません、
最初の起動に必要な時だけで、スタートした時に失敗したら --fresh を今回だけは
つけろ的なヒントをだしてください、スペックは全てレジュームで」 — every spec
resumes; a start failure is the one moment where a single fresh retry is the
right move. The failure report is where the operator is actually standing when
that moment arrives, so that is where the hint has to be.

THE TWO WAYS THIS GOES WRONG, and both are covered below:

  * MISSING — the report says the start failed and nothing about recovering.
    The operator's own instruction, unimplemented.
  * OVER-EAGER — the report names ``--fresh`` on a start that was ALREADY
    fresh. A fresh start cannot be wedged on a resumed session, so that is an
    error asserting a cause it never observed — the exact disease the sibling
    file next door documents. The negative test is paired with a positive
    control on the same message, so a blank or truncated report cannot pass it
    by accident.

  * And a third, quieter one: the hint reaching only the terminal. A
    false-negative start leaves no registry row, so whoever reads
    ``start_failure_diag.log`` tomorrow is often the only reader there is.

NO MOCKS (repo doctrine): the configs below are real objects driven through the
production entry point, and the exploding-config case is a real object whose
attribute access genuinely raises.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from scitex_agent_container._lifecycle._start_failure_diag import (
    fresh_retry_hint,
    raise_start_failure,
)


class _Claude:
    """The resolved ``spec.claude`` slice the hint reads."""

    def __init__(self, session: str) -> None:
        self.session = session


class _Cfg:
    """A real minimal config carrying a resolved session mode."""

    def __init__(self, name: str, session: str, runtime: str = "apptainer") -> None:
        self.name = name
        self.runtime = runtime
        self.claude = _Claude(session)


class _ExplodingCfg:
    """A config whose ``claude`` access genuinely raises.

    Not a hypothetical: ``raise_start_failure`` runs on whatever the caller was
    holding when the start blew up, and a diagnostic that raises over the
    failure it is describing destroys the report.
    """

    name = "exploding"

    @property
    def claude(self) -> Any:
        raise RuntimeError("resolved config was never populated")


def _unique(session: str) -> _Cfg:
    """An agent NOBODY else has started — its state dir cannot pre-exist.

    Same reason as the sibling file: sharing a name silently borrows another
    test's directory and makes these assertions depend on the schedule.
    """
    return _Cfg(name=f"freshhint-{uuid.uuid4().hex[:12]}", session=session)


def _diag_log(config: _Cfg) -> Path:
    from scitex_agent_container.runtimes.tui_session import state_dir_for_config

    return state_dir_for_config(config) / "start_failure_diag.log"


def _message(config: Any) -> str:
    """Drive the real failure path and return the operator-visible message."""
    with pytest.raises(RuntimeError) as excinfo:
        raise_start_failure(config)
    return str(excinfo.value)


def test_a_resuming_start_is_told_to_retry_with_fresh():
    """THE OPERATOR'S ASK. A resuming start that failed must name the escape hatch."""
    # Arrange
    config = _unique("continue")
    # Act
    message = _message(config)
    # Assert
    assert "--fresh" in message


def test_the_hint_names_the_agent_so_the_command_is_runnable():
    """A hint the operator has to assemble by hand is a hint they will mistype."""
    # Arrange
    config = _unique("continue")
    # Act
    message = _message(config)
    # Assert
    assert f"sac agents start {config.name} --fresh" in message


def test_the_hint_bounds_itself_to_one_retry():
    """``--fresh`` is a per-start override; making it the habit erases memory.

    The whole point of the operator's wording is the boundary — 「今回だけは」.
    An unqualified "try --fresh" is how a one-shot recovery becomes a spec edit.
    """
    # Arrange
    config = _unique("continue")
    # Act
    message = _message(config)
    # Assert
    assert "ONCE" in message


def test_the_hint_says_the_spec_stays_on_resume():
    """State the invariant positively, so the reader knows what NOT to change."""
    # Arrange
    config = _unique("continue")
    # Act
    message = _message(config)
    # Assert
    assert "spec stays on resume" in message


def test_an_already_fresh_start_still_reports_the_real_cause():
    """POSITIVE CONTROL for the negative test below.

    Without this, an empty or truncated message would satisfy "does not mention
    --fresh" while proving nothing at all.
    """
    # Arrange
    config = _unique("fresh")
    # Act
    message = _message(config)
    # Assert
    assert "runtime.start() returned False" in message


def test_an_already_fresh_start_is_not_told_to_retry_with_fresh():
    """A fresh start cannot be wedged on a resumed session.

    Naming ``--fresh`` here would be the report asserting a cause it never
    observed. Paired with the positive control directly above.
    """
    # Arrange
    config = _unique("fresh")
    # Act
    message = _message(config)
    # Assert
    assert "--fresh" not in message


def test_the_hint_reaches_the_persisted_record_too():
    """A remedy only the live terminal saw is lost to tomorrow's reader.

    The start that failed left no registry row; this log is often all there is.
    """
    # Arrange
    config = _unique("continue")
    log = _diag_log(config)
    # Act
    _message(config)
    # Assert
    assert "--fresh" in log.read_text()


def test_the_hint_never_raises_over_the_failure_it_describes():
    """A diagnostic that explodes destroys the report it exists to produce."""
    # Arrange
    config = _ExplodingCfg()
    # Act
    hint = fresh_retry_hint(config)
    # Assert
    assert isinstance(hint, str)


def test_an_unreadable_session_mode_still_offers_the_recovery():
    """Fail toward HELPING. Printing the hint needlessly costs a line;
    withholding it costs the operator the recovery."""
    # Arrange
    config = _ExplodingCfg()
    # Act
    hint = fresh_retry_hint(config)
    # Assert
    assert "--fresh" in hint
