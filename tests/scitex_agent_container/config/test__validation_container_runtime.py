"""``spec.container.runtime`` must advertise only implemented engines.

The enum is the surface a spec author plans against: a value listed here
reads as a supported way to run an agent. docker/podman were ripped out
2026-05-13, so accepting them advertised a capability that raised
ImportError at dispatch. Real ``validate_raw`` / real engine resolver,
no mocks.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scitex_agent_container.config._validation import (
    VALID_CONTAINER_RUNTIMES,
    validate_raw,
)
from scitex_agent_container.runtimes.claude_session import _container_runtime_for

_BASE = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "spec": {"runtime": "apptainer"},
}


def _runtime_errors(container_runtime):
    raw = {
        **_BASE,
        "spec": {**_BASE["spec"], "container": {"runtime": container_runtime}},
    }
    errors = validate_raw(raw, path="<test>")
    return [e for e in errors if "container.runtime" in e]


def test_accepted_container_runtimes_are_none_and_apptainer():
    # Arrange — the enum's exact membership is the contract.
    expected = frozenset({"none", "apptainer"})
    # Act
    accepted = VALID_CONTAINER_RUNTIMES
    # Assert
    assert accepted == expected


@pytest.mark.parametrize("value", sorted(VALID_CONTAINER_RUNTIMES))
def test_accepted_values_validate_cleanly(value):
    # Arrange — every enum member must survive its own validator.
    spec_value = value
    # Act
    bad = _runtime_errors(spec_value)
    # Assert
    assert bad == [], f"{value!r} is in the enum but the validator rejects it: {bad}"


@pytest.mark.parametrize("value", ["docker", "podman", "containerd", "banana"])
def test_unimplemented_engines_are_rejected(value):
    # Arrange — nothing implements these.
    spec_value = value
    # Act
    bad = _runtime_errors(spec_value)
    # Assert
    assert bad, f"{value!r} has no engine behind it but the validator accepted it"


def test_rejection_message_does_not_offer_docker():
    # Arrange — the error is the author's map of what actually exists.
    bad = _runtime_errors("docker")
    # Act
    offered = bad[0].split("got")[0]
    # Assert
    assert "docker" not in offered


def test_rejection_message_does_not_offer_podman():
    # Arrange
    bad = _runtime_errors("podman")
    # Act
    offered = bad[0].split("got")[0]
    # Assert
    assert "podman" not in offered


@pytest.mark.parametrize("value", sorted(VALID_CONTAINER_RUNTIMES - {"none"}))
def test_every_advertised_engine_resolves_to_a_real_runtime(value):
    """Fails if the enum gains a value no container engine implements."""
    # Arrange — the live resolver, not a second copy of the enum.
    config = SimpleNamespace(runtime=value)
    # Act
    runtime = _container_runtime_for(config)
    # Assert
    assert runtime is not None, (
        f"container.runtime={value!r} is accepted by the validator but "
        "_container_runtime_for resolves no engine for it"
    )
