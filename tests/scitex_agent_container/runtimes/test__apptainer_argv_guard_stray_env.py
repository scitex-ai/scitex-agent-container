#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""An env pair with no ``--env`` is a POSITIONAL, and apptainer calls it the image.

2026-08-18 incident. A bulk edit added the new board-identity variable to ~100
specs' ``raw_args`` and inserted only the VALUE, without the ``- --env`` that
has to precede it::

    - --env
    - SCITEX_TODO_AGENT_ID=business
    - SCITEX_CARDS_AGENT_ID=business      <- positional, in flag position

apptainer resolved that bare token against the launch directory and reported::

    FATAL: While checking container encryption: could not open image
      /home/ywatanabe/proj/business/SCITEX_CARDS_AGENT_ID=business

The message names an image nobody wrote, blames encryption, and never mentions
the missing flag. ``sac agents check`` had answered "Ready to deploy" on that
spec minutes earlier, because it validated ``raw_args`` as YAML and as a schema
and never as an apptainer argv.

WHY THE SIBLING GUARD CANNOT CATCH IT: :func:`validate_flag_argv` inspects
``_flag_region(argv)``, which by construction STOPS at the first positional. The
stray token does not break the flag region — it ENDS it. One guard finds a flag
with no value; this one finds a value with no flag.

WHY THE MATCH IS NARROW, and why that is the point: the detector fires only on
UPPER_SNAKE ``KEY=`` tokens. Value-shaped tokens legitimately follow flags this
module does not track (``--network-args portmap=8080:tcp``, ``--security
uid:1000``), and a broader rule would refuse launches it merely failed to
recognise — the failure mode this module's own doctrine forbids. Both of those
shapes are pinned below, so a later broadening trips a test.

Measured before shipping: across 117 live specs on compute-04, 103 declare
``raw_args`` and the detector flags NONE of them.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.runtimes._apptainer_argv_guard import (
    ApptainerArgvError,
    find_stray_env_pair,
    validate_raw_args,
)

#: The exact shape that took the agent down, reproduced verbatim.
INCIDENT = [
    "--env",
    "SCITEX_TODO_AGENT_ID=business",
    "SCITEX_CARDS_AGENT_ID=business",
    "--env",
    "SCITEX_AGENT_CONTAINER_STATE_DB=/state/business/state.db",
]

#: The same spec after the missing flag is restored.
REPAIRED = [
    "--env",
    "SCITEX_TODO_AGENT_ID=business",
    "--env",
    "SCITEX_CARDS_AGENT_ID=business",
    "--env",
    "SCITEX_AGENT_CONTAINER_STATE_DB=/state/business/state.db",
]


def _message_for(raw_args, **kw) -> str:
    """The refusal text for ``raw_args``. Fails loudly if it does NOT refuse.

    Exists because STX-TQ007 counts a ``pytest.raises`` block as an assertion,
    so a test that raises and then inspects the message would carry two. The
    raise belongs here; what the message SAYS is what each test asserts.
    """
    with pytest.raises(ApptainerArgvError) as excinfo:
        validate_raw_args(raw_args, **kw)
    return str(excinfo.value)


def test_the_incident_shape_is_detected():
    # Arrange — THE REGRESSION.
    raw_args = INCIDENT
    # Act
    found = find_stray_env_pair(raw_args)
    # Assert
    assert found is not None


def test_the_detector_names_the_offending_token():
    # Arrange — a guard that says "something is wrong" makes the reader
    # re-derive what. Name the token.
    raw_args = INCIDENT
    # Act
    _, token = find_stray_env_pair(raw_args)
    # Assert
    assert token == "SCITEX_CARDS_AGENT_ID=business"


def test_the_detector_names_the_index_so_the_line_is_findable():
    # Arrange — raw_args is a YAML list; the index IS the line to open.
    raw_args = INCIDENT
    # Act
    index, _ = find_stray_env_pair(raw_args)
    # Assert
    assert index == 2


def test_the_repaired_spec_is_silent():
    # Arrange — POSITIVE CONTROL for every negative below: the detector
    # must go quiet once the flag is restored, or "silent" proves nothing.
    raw_args = REPAIRED
    # Act
    found = find_stray_env_pair(raw_args)
    # Assert
    assert found is None


@pytest.mark.parametrize(
    "raw_args",
    [
        pytest.param(["--network-args", "portmap=8080:tcp"], id="network-args-value"),
        pytest.param(["--security", "uid:1000"], id="security-value"),
        pytest.param(["--app", "myapp"], id="app-value"),
        pytest.param(["--hostname", "box=1"], id="hostname-value-with-equals"),
    ],
)
def test_no_false_positive_on_values_of_untracked_flags(raw_args):
    # Arrange — VALUE_TAKING_FLAGS is a deliberate SUBSET, so values of
    # flags outside it look like positionals. Refusing those would block
    # launches the guard merely failed to recognise.
    # Act
    found = find_stray_env_pair(raw_args)
    # Assert
    assert found is None


def test_an_empty_raw_args_is_silent():
    # Arrange — most specs declare none at all.
    raw_args = []
    # Act
    found = find_stray_env_pair(raw_args)
    # Assert
    assert found is None


def test_a_none_raw_args_is_silent():
    # Arrange — the field is optional; absent must not be an error.
    raw_args = None
    # Act
    found = find_stray_env_pair(raw_args)
    # Assert
    assert found is None


def test_validate_raises_on_the_incident_shape():
    # Arrange — the callable the CLI and the launch path use.
    raw_args = INCIDENT
    # Act / Assert
    with pytest.raises(ApptainerArgvError):
        validate_raw_args(raw_args)


def test_validate_is_a_no_op_on_the_repaired_shape():
    # Arrange — POSITIVE CONTROL for the raise above.
    raw_args = REPAIRED
    # Act
    result = validate_raw_args(raw_args)
    # Assert
    assert result is None


def test_the_message_shows_the_exact_two_yaml_lines_to_write():
    # Arrange — the operator is looking at a YAML list. Hand them the
    # lines, not a rule to apply.
    raw_args = INCIDENT
    # Act
    message = _message_for(raw_args)
    # Assert
    assert "- --env\n    - SCITEX_CARDS_AGENT_ID=business" in message


def test_the_message_explains_what_apptainer_does_with_it():
    # Arrange — the real FATAL blames encryption and names a path nobody
    # wrote. Say what actually happens, or the reader chases the path.
    raw_args = INCIDENT
    # Act
    message = _message_for(raw_args)
    # Assert
    assert "IMAGE PATH" in message


def test_the_message_names_the_agent_when_it_is_known():
    # Arrange — the sibling guard's own documented lesson: an operator
    # opened the wrong spec because the message never named the file.
    raw_args = INCIDENT
    # Act
    message = _message_for(raw_args, agent="business")
    # Assert
    assert "'business'" in message
