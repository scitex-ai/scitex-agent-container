"""Tests for scitex_agent_container.config._startup_command_validation.

Deterministic recurrence guard for the 2026-07-16 P0: every one of the
fleet's 96 generated specs shipped an UNGUARDED::

    rm -rf $HOME/proj 2>/dev/null; ln -sfn <src> $HOME/proj

in ``startup_commands`` — one bad symlink from recursively deleting the
~195 real repos under ``$HOME/proj``. The validator REJECTS a
recursive-force ``rm`` on a VARIABLE target at spec-validate time; it
ALLOWS the fixed, NON-recursive form ``[ -L "$VAR" ] && rm -f "$VAR"``.

Real validator, no mocks. AAA + one logical assert per test (PA-307).
The four task-mandated cases run through the real ``validate_raw`` on a
complete, otherwise-valid spec (integration); the flag-ordering matrix +
false-positive allow-list drive the sibling ``validate_startup_commands``
directly (the same function ``validate_raw`` calls).
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._startup_command_validation import (
    validate_startup_commands,
)
from scitex_agent_container.config._validation import validate_raw

# ---------------------------------------------------------------------------
# Spec builders
# ---------------------------------------------------------------------------


def _complete_spec() -> dict:
    """Fully-explicit spec (red-start ruling 2026-07-21: EVERY field)."""
    from tests.scitex_agent_container._helpers.explicit_spec import (
        explicit_spec,
    )

    return {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": explicit_spec(
            {
                "runtime": "tui",
                "host": "${HOSTNAME}",
                "workdir": "/home/agent/work",
                "apptainer": {"image": "/x.sif", "binds": []},
                "claude": {"model": "opus"},
                "health": {"enabled": True, "interval": 60},
                "restart": {"policy": "on-failure", "max_retries": 3},
            }
        ),
    }


_COMPLETE_SPEC = _complete_spec()


def _raw_with_startup(cmds: list) -> dict:
    """A complete, valid v3 spec whose only variable is ``startup_commands``."""
    import copy

    raw = copy.deepcopy(_COMPLETE_SPEC)
    raw["spec"]["startup_commands"] = cmds
    return raw


def _startup_errors_via_validate_raw(cmds: list) -> list[str]:
    """Errors from the REAL validator, narrowed to startup_commands ones."""
    errors = validate_raw(_raw_with_startup(cmds), path="<test>")
    return [e for e in errors if "startup_commands" in e]


# ---------------------------------------------------------------------------
# The four task-mandated cases — through the real validate_raw
# ---------------------------------------------------------------------------

_LANDMINE = "rm -rf $HOME/proj 2>/dev/null; ln -sfn /src/proj $HOME/proj"
_GUARDED = '[ -L "$HOME/proj" ] && rm -f "$HOME/proj"; ln -sfn /src/proj "$HOME/proj"'


def test_landmine_rm_rf_variable_is_rejected() -> None:
    # Arrange — the exact unguarded form the 96 specs shipped.
    cmds = [{"command": _LANDMINE}]
    # Act
    startup_errors = _startup_errors_via_validate_raw(cmds)
    # Assert
    assert startup_errors, f"landmine {_LANDMINE!r} must be rejected"


def test_landmine_rejection_echoes_the_offending_command() -> None:
    # Arrange
    cmds = [{"command": _LANDMINE}]
    # Act
    startup_errors = _startup_errors_via_validate_raw(cmds)
    # Assert — the error names the offending command verbatim.
    assert _LANDMINE in startup_errors[0]


def test_landmine_rejection_shows_the_guarded_fix() -> None:
    # Arrange
    cmds = [{"command": _LANDMINE}]
    # Act
    startup_errors = _startup_errors_via_validate_raw(cmds)
    # Assert — actionable: the message hands over the symlink-checked fix.
    assert 'rm -f "$HOME/proj"' in startup_errors[0]


def test_guarded_form_passes() -> None:
    # Arrange — symlink-checked + NON-recursive ``rm -f`` is the fix we
    # shipped; it must validate clean even though it targets $HOME/proj.
    cmds = [{"command": _GUARDED}]
    # Act
    startup_errors = _startup_errors_via_validate_raw(cmds)
    # Assert
    assert startup_errors == []


def test_literal_path_rm_rf_passes() -> None:
    # Arrange — a fixed literal target is the author's explicit choice; no
    # variable can silently resolve to the wrong tree, so no false positive.
    cmds = [{"command": "rm -rf /tmp/fixed/dir"}]
    # Act
    startup_errors = _startup_errors_via_validate_raw(cmds)
    # Assert
    assert startup_errors == []


def test_braced_variable_is_rejected() -> None:
    # Arrange — ${SCRATCH}/build is a braced variable target.
    cmds = [{"command": "rm -rf ${SCRATCH}/build"}]
    # Act
    startup_errors = _startup_errors_via_validate_raw(cmds)
    # Assert
    assert startup_errors, "braced ${SCRATCH} target must be rejected"


def test_complete_spec_without_startup_commands_has_no_errors() -> None:
    # Arrange — sanity: the guard adds ZERO errors when the field is absent.
    raw = _COMPLETE_SPEC
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert errors == []


# ---------------------------------------------------------------------------
# Flag-ordering matrix — every common recursive-force spelling is caught
# ---------------------------------------------------------------------------

_REJECTED_COMMANDS = [
    "rm -rf $HOME/proj",  # canonical
    "rm -fr $VAR",  # force-then-recursive cluster
    "rm -Rf $VAR",  # capital -R
    "rm -r -f $VAR",  # separated short flags
    "rm -f -r $VAR",  # separated, reversed
    "rm --recursive --force $VAR",  # long flags
    "rm -rf ${SCRATCH}/build",  # braced variable
    "rm -rf ~/proj",  # leading tilde (home expansion)
    "rm -rf -- $VAR",  # POSIX end-of-options marker
    "rm -rf /tmp/keep $HOME/proj",  # mixed: one variable target present
    "/bin/rm -rf $HOME/x",  # absolute-path rm is equally dangerous
    "FOO=bar rm -rf $HOME/x",  # leading env-assignment stripped
    "mkdir -p /tmp/x && rm -rf $HOME/proj",  # rm in second segment
]


@pytest.mark.parametrize("command", _REJECTED_COMMANDS)
def test_recursive_force_variable_is_rejected(command: str) -> None:
    # Arrange
    spec = {"startup_commands": [{"command": command}]}
    # Act
    errors = validate_startup_commands(spec)
    # Assert
    assert errors, f"expected rejection for {command!r}"


# ---------------------------------------------------------------------------
# False-positive allow-list — the matcher must NOT over-reject
# ---------------------------------------------------------------------------

_ALLOWED_COMMANDS = [
    'rm -f "$HOME/proj"',  # FIXED form: force but NON-recursive
    "rm -f $VAR",  # non-recursive, variable target
    "rm -rf /tmp/fixed/dir",  # recursive-force but LITERAL target
    "rm -rf /opt/build/cache",  # literal
    "echo rm -rf $HOME/proj",  # rm is an ARGUMENT to echo, not invoked
    'printf "rm -rf $X"',  # rm inside a printf argument
    "rm -r $VAR",  # recursive WITHOUT force (task rule: force required)
    "ln -sfn /src/proj $HOME/proj",  # the symlink half of the pattern
    "true",  # trivial no-op
]


@pytest.mark.parametrize("command", _ALLOWED_COMMANDS)
def test_safe_commands_are_not_flagged(command: str) -> None:
    # Arrange
    spec = {"startup_commands": [{"command": command}]}
    # Act
    errors = validate_startup_commands(spec)
    # Assert
    assert errors == [], f"false positive on {command!r}: {errors}"


# ---------------------------------------------------------------------------
# Defensive shapes — collapse to "nothing to check"
# ---------------------------------------------------------------------------


def test_missing_startup_commands_yields_no_error() -> None:
    # Arrange
    spec: dict = {}
    # Act
    errors = validate_startup_commands(spec)
    # Assert
    assert errors == []


def test_non_list_startup_commands_yields_no_error() -> None:
    # Arrange — wrong shape; downstream validators own the type error.
    spec = {"startup_commands": {"command": "rm -rf $X"}}
    # Act
    errors = validate_startup_commands(spec)
    # Assert
    assert errors == []


def test_non_dict_entry_is_skipped() -> None:
    # Arrange — a bare-string entry (parser drops it anyway).
    spec = {"startup_commands": ["rm -rf $X"]}
    # Act
    errors = validate_startup_commands(spec)
    # Assert
    assert errors == []


def test_non_string_command_is_skipped() -> None:
    # Arrange
    spec = {"startup_commands": [{"command": 42}]}
    # Act
    errors = validate_startup_commands(spec)
    # Assert
    assert errors == []


def test_error_names_the_entry_index() -> None:
    # Arrange — first entry OK, second entry is the landmine; index pins 1.
    spec = {
        "startup_commands": [
            {"command": "echo starting"},
            {"command": "rm -rf $HOME/proj"},
        ]
    }
    # Act
    errors = validate_startup_commands(spec)
    # Assert
    assert "startup_commands[1]" in errors[0]
