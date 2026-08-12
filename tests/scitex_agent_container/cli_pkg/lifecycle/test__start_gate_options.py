"""Tests for the ``sac agents start`` spec-gate flags.

These exist because of a defect that EVERY other test in this change missed.
The gates default to refusing, and ``_resolve_strict_drift`` treats an explicit
``False`` as "the caller demanded leniency". A click flag's natural default is
``False`` — so wiring ``--strict-drift`` straight through would have had the
CLI hand the resolver an explicit ``False`` on every single start and silently
disable the gate, while the unit tests (which call ``agent_start`` without the
keyword, i.e. ``None``) all stayed green.

So the fact under test is not "the flag works". It is: **an absent flag must
mean NO INSTRUCTION, not an instruction to be lenient.**

PA-306 no-mocks: the real click callbacks and the real command object.
STX-TQ002/TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

import click

from scitex_agent_container._drift._local import ALLOW_STALE_ENV
from scitex_agent_container._lifecycle._layers_preflight import ALLOW_ENV
from scitex_agent_container._lifecycle._start_preflight import _resolve_strict_drift
from scitex_agent_container.cli_pkg.lifecycle._start_gate_options import (
    _set_env_when_given,
    _true_or_unset,
    spec_gate_options,
)


def _params(func) -> "dict[str, click.Parameter]":
    """The click parameters the decorator attached, by name/flag."""
    command = click.command()(func)
    return {p.name or p.opts[0]: p for p in command.params}


@click.command()
def _bare():  # pragma: no cover - only its parameter list is inspected
    pass


class TestAbsentFlagMeansNoInstruction:
    """The defect this file exists for."""

    def test_unpassed_strict_drift_resolves_to_none(self):
        # Arrange — click hands a bare flag ``False`` when it is not passed.
        # Act
        resolved = _true_or_unset(None, None, False)
        # Assert — None, NOT False: "no instruction", not "be lenient".
        assert resolved is None

    def test_passed_strict_drift_resolves_to_true(self):
        # Arrange
        # Act
        resolved = _true_or_unset(None, None, True)
        # Assert
        assert resolved is True

    def test_the_cli_default_still_refuses_a_stale_spec(self, env_save_restore):
        # Arrange — the end-to-end shape of the bug: what the CLI passes when
        # NO flag is given must still reach the resolver as STRICT.
        env_save_restore.delete("SAC_ALLOW_STALE_SPEC")
        env_save_restore.delete("SCITEX_AGENT_CONTAINER_ALLOW_STALE_SPEC")
        env_save_restore.delete("SAC_STRICT_DRIFT")
        env_save_restore.delete("SCITEX_AGENT_CONTAINER_STRICT_DRIFT")
        # Act
        resolved = _resolve_strict_drift(_true_or_unset(None, None, False))
        # Assert
        assert resolved is True


class TestOverridesSetTheirEnvVar:
    """Both overrides travel as env vars so the parallel path — one SUBPROCESS
    per agent — inherits them. A local variable would not survive that."""

    def test_allow_stale_callback_sets_its_env_var(self, env_save_restore):
        # Arrange
        env_save_restore.delete(ALLOW_STALE_ENV)
        callback = _set_env_when_given(ALLOW_STALE_ENV)
        # Act
        callback(None, None, True)
        # Assert
        import os

        assert os.environ[ALLOW_STALE_ENV] == "1"

    def test_allow_layers_callback_sets_its_env_var(self, env_save_restore):
        # Arrange
        env_save_restore.delete(ALLOW_ENV)
        callback = _set_env_when_given(ALLOW_ENV)
        # Act
        callback(None, None, True)
        # Assert
        import os

        assert os.environ[ALLOW_ENV] == "1"

    def test_absent_flag_leaves_an_exported_env_var_alone(self, env_save_restore):
        # Arrange — `SAC_ALLOW_STALE_SPEC=1 sac agents start x` must behave the
        # same as the export two lines earlier in a shell script.
        env_save_restore.set(ALLOW_STALE_ENV, "1")
        callback = _set_env_when_given(ALLOW_STALE_ENV)
        # Act
        callback(None, None, False)
        # Assert
        import os

        assert os.environ[ALLOW_STALE_ENV] == "1"


class TestFlagsAreAttached:
    """All three flags reach the command, and only ``--strict-drift`` is passed
    to the function body — the two overrides are ``expose_value=False``."""

    def test_strict_drift_is_exposed_to_the_command(self):
        # Arrange
        # Act
        names = _params(lambda **kw: None)
        # Assert
        assert "strict_drift" not in names  # undecorated control

    def test_decorator_exposes_strict_drift(self):
        # Arrange
        decorated = spec_gate_options(lambda **kw: None)
        # Act
        names = _params(decorated)
        # Assert
        assert "strict_drift" in names

    def test_decorator_attaches_the_allow_stale_flag(self):
        # Arrange
        decorated = spec_gate_options(lambda **kw: None)
        # Act
        flags = [opt for p in _params(decorated).values() for opt in p.opts]
        # Assert
        assert "--allow-stale-spec" in flags

    def test_decorator_attaches_the_allow_layers_flag(self):
        # Arrange
        decorated = spec_gate_options(lambda **kw: None)
        # Act
        flags = [opt for p in _params(decorated).values() for opt in p.opts]
        # Assert
        assert "--allow-undeclared-layers" in flags

    def test_overrides_are_not_passed_to_the_command_body(self):
        # Arrange — expose_value=False; they travel by env, not by keyword.
        decorated = spec_gate_options(lambda **kw: None)
        # Act
        exposed = [p.name for p in _params(decorated).values() if p.expose_value]
        # Assert
        assert exposed == ["strict_drift"]
