"""Fork contract tests — these EXERCISE the verb, not just import it.

Regression guard for the Sep-2026 incident where ``sac agents fork``
failed at runtime (``ModuleNotFoundError`` for the implementation
module) under a green suite: the CLI imported cleanly because the
broken reference sat inside a function body, and no test invoked the
fork path. Every test here drives :func:`prepare_fork_spawn` or
:func:`derive_fork_spec` against a fixture parent doc and asserts the
fork contract a live spawn depends on.
"""

import os
from functools import reduce

import pytest
import yaml

from scitex_agent_container._lifecycle._fork import (
    ForkSeedError,
    build_fork_boot_kick,
    derive_fork_spec,
    prepare_fork_spawn,
    resolve_fork_name,
)

_PARENT_DOC = {
    "spec": {
        "harness": "hermes",
        "model": "muse-spark-1.3-contributor",
        "workdir": "/work",
        "available_harnesses": {"hermes": {}},
        "available_engines": {
            "scitex-free": {
                "model": "muse-spark-1.3-contributor",
                "reasoning_effort": "xhigh",
            }
        },
        "apptainer": {
            "env": {
                "SCITEX_CARDS_AGENT_ID": "parent-agent",
                "SAC_NAME": "parent-agent",
                "SCITEX_TODO_AGENT_ID": "parent-agent",
            }
        },
        "restart": {"policy": "always"},
        "a2a": {"port": 19001},
        "comms": {"channels": ["server:claude-code-telegrammer"]},
        "startup_prompts": ["parent prompt"],
    },
    "metadata": {"labels": {"role": "lead", "groups": ["lead"]}},
}


@pytest.mark.parametrize(
    "requested,existing,expected",
    [
        (None, [], "p-fork"),
        (None, ["p-fork"], "p-fork-2"),
        ("custom", ["custom"], "custom"),
    ],
)
def test_resolve_fork_name_defaults_and_bumps(requested, existing, expected):
    # Arrange
    parent = "p"
    # Act
    name = resolve_fork_name(parent, requested, existing)
    # Assert
    assert name == expected


@pytest.fixture
def derived():
    return derive_fork_spec(
        _PARENT_DOC,
        fork_name="p-fork",
        parent_name="p",
        persist=False,
        task="green the audit",
    )


@pytest.mark.parametrize(
    "path,expected",
    [
        (("spec", "apptainer", "env", "SCITEX_CARDS_AGENT_ID"), "p-fork"),
        (("spec", "apptainer", "env", "SAC_FORK_PARENT"), "p"),
        (("spec", "restart", "policy"), "never"),
        (("spec", "a2a", "port"), "auto"),
        (
            ("spec", "extensions", "fork"),
            {
                "parent": "p",
                "task": "green the audit",
                "fresh": False,
                "handover": False,
            },
        ),
        (("metadata", "labels", "role"), "domain-subagent"),
        (("metadata", "labels", "parent"), "p"),
    ],
)
def test_derive_fork_spec_splits_identity(derived, path, expected):
    # Arrange
    doc = derived
    # Act
    value = reduce(lambda node, key: node[key], path, doc)
    # Assert
    assert value == expected


@pytest.mark.parametrize(
    "path,retired",
    [
        (("spec",), "env"),
        (("spec",), "claude"),
        (("spec", "apptainer", "env"), "SAC_NAME"),
        (("spec", "apptainer", "env"), "SCITEX_TODO_AGENT_ID"),
        (("spec", "comms", "channels"), "server:claude-code-telegrammer"),
    ],
)
def test_fork_does_not_inherit_parent_identity_surfaces(derived, path, retired):
    # Arrange
    doc = derived
    # Act
    surface = reduce(lambda node, key: node[key], path, doc)
    # Assert
    assert retired not in surface


@pytest.mark.parametrize(
    "path,expected",
    [
        (("spec", "restart", "policy"), "always"),
        (("metadata", "labels", "role"), "fix-manager"),
    ],
)
def test_derive_fork_spec_persist_and_role(path, expected):
    # Arrange
    # Act
    doc = derive_fork_spec(
        _PARENT_DOC,
        fork_name="p-fork",
        parent_name="p",
        persist=True,
        role="fix-manager",
        task="t",
    )
    # Assert
    assert reduce(lambda node, key: node[key], path, doc) == expected


def test_derive_fork_spec_requires_task():
    # Arrange
    # Act
    # Assert
    with pytest.raises(ForkSeedError):
        derive_fork_spec(
            _PARENT_DOC, fork_name="p-fork", parent_name="p", persist=False, task=" "
        )


def test_derive_fork_spec_rejects_non_mapping():
    # Arrange
    # Act
    # Assert
    with pytest.raises(ForkSeedError):
        derive_fork_spec(
            {"spec": "not-a-mapping"},  # type: ignore[dict-item]
            fork_name="p-fork",
            parent_name="p",
            persist=False,
            task="t",
        )


@pytest.mark.parametrize("required", ["assignee=p", "do the thing", "p-fork"])
def test_boot_kick_states_identity_contract(required):
    # Arrange
    # Act
    kick = build_fork_boot_kick("p-fork", "p", "do the thing")
    # Assert
    assert required in kick


@pytest.fixture
def registry(tmp_path):
    """Exercise the real resolver in an explicitly selected private registry."""
    state = tmp_path / "state"
    parent = state / "agent-container/agents/p/spec.yaml"
    parent.parent.mkdir(parents=True)
    parent.write_text(yaml.safe_dump(_PARENT_DOC))
    saved = {key: os.environ.get(key) for key in ("SCITEX_DIR", "SAC_AGENT_SCOPE")}
    os.environ.update(SCITEX_DIR=str(state), SAC_AGENT_SCOPE="user")
    try:
        yield parent.parent.parent
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_prepare_fork_spawn_unknown_parent_fails_loud(registry):
    # Arrange
    parent = "no-such-agent"
    # Act
    # Assert
    with pytest.raises(ForkSeedError, match="cannot resolve parent"):
        prepare_fork_spawn(parent, task="t")


def test_prepare_fork_spawn_taken_name_fails_loud(registry):
    # Arrange
    taken = registry / "taken-name/spec.yaml"
    taken.parent.mkdir()
    taken.write_text(yaml.safe_dump(_PARENT_DOC))
    # Act
    # Assert
    with pytest.raises(ForkSeedError, match="already taken"):
        prepare_fork_spawn("p", fork_name="taken-name", task="t")


@pytest.mark.parametrize(
    "path,expected",
    [
        ((0,), "p-fork"),
        ((1, "spec", "apptainer", "env", "SCITEX_CARDS_AGENT_ID"), "p-fork"),
        ((1, "spec", "a2a", "port"), "auto"),
    ],
)
def test_prepare_fork_spawn_derives_inline_doc(registry, path, expected):
    # Arrange
    parent = "p"
    # Act
    result = prepare_fork_spawn(parent, task="green the audit")
    # Assert
    assert reduce(lambda node, key: node[key], path, result) == expected


def test_prepare_fork_reuses_the_real_parent_capability_directory(registry):
    # Arrange
    capability = registry / "p/to_home"
    capability.mkdir()
    # Act
    name, doc = prepare_fork_spawn("p", task="green the audit")
    # Assert
    assert doc["spec"]["to_home"] == str(capability)


def test_derived_doc_passes_validator_fork_surfaces():
    """End-to-end guard for the live-spawn failure: the derived doc must
    not trip the v3 validator on fork-touched surfaces (top-level env,
    legacy claude surface). Runs the REAL validator, not a shape
    assertion, so future validator drift fails here first."""
    # Arrange
    from scitex_agent_container.config._validation import validate_raw

    doc = derive_fork_spec(
        _PARENT_DOC,
        fork_name="p-fork",
        parent_name="p",
        persist=False,
        task="green the audit",
    )
    errors = validate_raw(doc, "fork-verify/spec.yaml")
    # Act
    fork_surface_errors = [
        e
        for e in errors
        if "no longer accepted at the top level" in e or "declare one surface" in e
    ]
    # Assert
    assert fork_surface_errors == []


@pytest.mark.parametrize("required", ["HANDOVER from p", "db is down, use cache"])
def test_boot_kick_carries_handover(required):
    # Arrange
    # Act
    kick = build_fork_boot_kick(
        "p-fork", "p", "do it", handover="db is down, use cache"
    )
    # Assert
    assert required in kick


@pytest.mark.parametrize("key", ["fresh", "handover"])
def test_fresh_marks_extension(key):
    # Arrange
    # Act
    doc = derive_fork_spec(
        _PARENT_DOC,
        fork_name="p-fork",
        parent_name="p",
        persist=False,
        task="t",
        fresh=True,
        handover="note",
    )
    # Assert
    assert doc["spec"]["extensions"]["fork"][key] is True


def test_fresh_sets_every_inherited_harness_session_to_fresh():
    # Arrange
    parent = _PARENT_DOC
    # Act
    doc = derive_fork_spec(
        parent, fork_name="p-fork", parent_name="p", persist=False, task="t", fresh=True
    )
    modes = {
        entry.get("session", {}).get("mode")
        for entry in doc["spec"]["available_harnesses"].values()
    }
    # Assert
    assert modes == {"fresh"}


def test_nonfresh_inherits_session_modes():
    # Arrange
    # Act
    doc = derive_fork_spec(
        _PARENT_DOC,
        fork_name="p-fork",
        parent_name="p",
        persist=False,
        task="t",
    )
    # Assert
    assert doc["spec"]["extensions"]["fork"]["fresh"] is False
