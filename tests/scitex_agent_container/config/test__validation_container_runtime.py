"""``spec.container.runtime`` is REMOVED — the engine is not a choice.

This file used to pin the enum ``{none, apptainer}`` and assert that every
advertised engine resolved to a real runtime. That contract is gone with
the field (operator ruling 2026-08-14: abolish the CHOICE, not just the
alternatives), and the old tests carried the confusion that made the field
survive this long — ``test_every_advertised_engine_resolves_to_a_real_runtime``
fed a *container*-engine value into ``SimpleNamespace(runtime=...)``, i.e.
the TOP-LEVEL ``spec.runtime`` launch-mode field, which is a different axis
that merely shares the name. It also excluded ``"none"`` from that check —
the one value 105 of 105 live specs actually wrote.

What is pinned now: the key is rejected on PRESENCE, whatever its value;
nothing in the config tree still carries an engine field; and a freshly
scaffolded spec cannot reproduce the key. Real ``validate_raw``, no mocks.
"""

from __future__ import annotations

import dataclasses

import pytest

from scitex_agent_container.config._container_engine import CONTAINER_ENGINE
from scitex_agent_container.config._explicit_validation import explicit_spec_defaults
from scitex_agent_container.config._types import ContainerSpec
from scitex_agent_container.config._validation import validate_raw

_BASE = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "spec": {"runtime": "apptainer"},
}


def _runtime_errors(container_block):
    raw = {**_BASE, "spec": {**_BASE["spec"], "container": container_block}}
    errors = validate_raw(raw, path="<test>")
    return [e for e in errors if "container.runtime" in e]


def test_the_engine_is_apptainer():
    # Arrange — the constant IS the containment guarantee; pin its value.
    expected = "apptainer"
    # Act
    actual = CONTAINER_ENGINE
    # Assert
    assert actual == expected


# Every value a live spec could plausibly carry, INCLUDING the ones the old
# check waved through: 'none' was in the enum, and '' / None slipped past
# `if cr and cr not in ...` entirely.
@pytest.mark.parametrize(
    "value", ["none", "apptainer", "docker", "podman", "kubernetes", "", None, 0, False]
)
def test_declaring_the_key_is_rejected_whatever_the_value(value):
    # Arrange — presence is the offence, not the value.
    block = {"runtime": value}
    # Act
    errors = _runtime_errors(block)
    # Assert
    assert errors, f"container.runtime={value!r} was accepted; it must be rejected"


def test_an_empty_or_null_value_does_not_pass_unexamined():
    """The exact shape the previous ``if cr and ...`` check let through."""
    # Arrange
    written_but_empty = {"runtime": None}
    # Act
    errors = _runtime_errors(written_but_empty)
    # Assert
    assert errors, "a written-but-empty runtime: key still declares the field"


def test_a_container_block_without_the_key_is_clean():
    # Arrange — the post-migration shape every spec must reach.
    block = {"image": "sac.sif", "network": "host", "mount_host_claude": False}
    # Act
    errors = _runtime_errors(block)
    # Assert
    assert errors == []


def test_the_rejection_tells_the_author_to_delete_the_line():
    # Arrange — the hint must clear its own gate.
    message = _runtime_errors({"runtime": "none"})[0]
    # Act
    says_delete = "DELETE" in message and "runtime:" in message
    # Assert
    assert says_delete, message


def test_the_rejection_names_the_engine_that_runs_anyway():
    # Arrange
    message = _runtime_errors({"runtime": "none"})[0]
    # Act
    names_engine = CONTAINER_ENGINE in message
    # Assert
    assert names_engine, message


def test_the_rejection_offers_no_replacement_engine():
    # Arrange — a removal that suggests an alternative rebuilds the menu.
    message = _runtime_errors({"runtime": "docker"})[0]
    # Act
    offered = message.lower()
    # Assert
    assert "podman" not in offered, message


def test_the_rejection_is_not_a_value_complaint():
    # Arrange — "must be X" would mean the field still has a legal value.
    message = _runtime_errors({"runtime": "docker"})[0]
    # Act
    reads_as_enum_error = "must be" in message.lower()
    # Assert
    assert not reads_as_enum_error, message


def test_container_spec_carries_no_engine_field():
    # Arrange — the dataclass is the value tree's SSoT.
    names = {f.name for f in dataclasses.fields(ContainerSpec)}
    # Act
    has_engine = "runtime" in names
    # Assert
    assert not has_engine, f"ContainerSpec still models an engine: {sorted(names)}"


def test_a_scaffolded_spec_cannot_reproduce_the_key():
    """The paste-ready block is where a removed field comes back to life."""
    # Arrange — what the explicit-fields hint / `agents create` would emit.
    defaults = explicit_spec_defaults("Agent")
    # Act
    container = defaults.get("container", {})
    # Assert
    assert "runtime" not in container, (
        "the required/paste-ready block still emits container.runtime — a "
        "spec scaffolded from it would be born failing validation"
    )
