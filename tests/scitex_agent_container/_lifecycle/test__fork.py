"""Fork contract tests — these EXERCISE the verb, not just import it.

Regression guard for the Sep-2026 incident where ``sac agents fork``
failed at runtime (``ModuleNotFoundError`` for the implementation
module) under a green suite: the CLI imported cleanly because the
broken reference sat inside a function body, and no test invoked the
fork path. Every test here drives :func:`prepare_fork_spawn` or
:func:`derive_fork_spec` against a fixture parent doc and asserts the
fork contract a live spawn depends on.
"""

import pytest

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


def test_resolve_fork_name_defaults_and_bumps():
    assert resolve_fork_name("p", None, []) == "p-fork"
    assert resolve_fork_name("p", None, ["p-fork"]) == "p-fork-2"
    assert resolve_fork_name("p", "custom", ["custom"]) == "custom"


def test_derive_fork_spec_splits_identity():
    doc = derive_fork_spec(
        _PARENT_DOC,
        fork_name="p-fork",
        parent_name="p",
        persist=False,
        task="green the audit",
    )
    spec = doc["spec"]
    assert "env" not in spec
    assert "claude" not in spec
    assert spec["apptainer"]["env"]["SCITEX_CARDS_AGENT_ID"] == "p-fork"
    assert spec["apptainer"]["env"]["SAC_FORK_PARENT"] == "p"
    assert "SAC_NAME" not in spec["apptainer"]["env"]
    assert "SCITEX_TODO_AGENT_ID" not in spec["apptainer"]["env"]
    assert spec["restart"]["policy"] == "never"
    assert spec["a2a"]["port"] == "auto"
    assert "server:claude-code-telegrammer" not in spec["comms"]["channels"]
    assert spec["extensions"]["fork"] == {"parent": "p", "task": "green the audit"}
    assert doc["metadata"]["labels"]["role"] == "domain-subagent"
    assert doc["metadata"]["labels"]["parent"] == "p"
    kick = spec["startup_prompts"][0]
    assert "p-fork" in kick and "green the audit" in kick


def test_derive_fork_spec_persist_and_role():
    doc = derive_fork_spec(
        _PARENT_DOC,
        fork_name="p-fork",
        parent_name="p",
        persist=True,
        role="fix-manager",
        task="t",
    )
    assert doc["spec"]["restart"]["policy"] == "always"
    assert doc["metadata"]["labels"]["role"] == "fix-manager"


def test_derive_fork_spec_requires_task():
    with pytest.raises(ForkSeedError):
        derive_fork_spec(
            _PARENT_DOC, fork_name="p-fork", parent_name="p", persist=False, task=" "
        )


def test_derive_fork_spec_rejects_non_mapping():
    with pytest.raises(ForkSeedError):
        derive_fork_spec(
            {"spec": "not-a-mapping"},  # type: ignore[dict-item]
            fork_name="p-fork",
            parent_name="p",
            persist=False,
            task="t",
        )


def test_boot_kick_states_identity_contract():
    kick = build_fork_boot_kick("p-fork", "p", "do the thing")
    assert "assignee=p" in kick
    assert "do the thing" in kick


def test_prepare_fork_spawn_unknown_parent_fails_loud(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "scitex_agent_container._lifecycle._fork.resolve_config",
        lambda name: (_ for _ in ()).throw(KeyError(name)),
    )
    with pytest.raises(ForkSeedError, match="cannot resolve parent"):
        prepare_fork_spawn("no-such-agent", task="t")


def test_prepare_fork_spawn_taken_name_fails_loud(tmp_path, monkeypatch):
    import yaml

    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(_PARENT_DOC))
    monkeypatch.setattr(
        "scitex_agent_container._lifecycle._fork.resolve_config",
        lambda name: str(spec_path),
    )
    monkeypatch.setattr(
        "scitex_agent_container._lifecycle._fork.enumerate_agent_names",
        lambda: ["taken-name"],
    )
    monkeypatch.setattr(
        "scitex_agent_container._lifecycle._fork._resolve_parent_to_home",
        lambda path, doc: None,
    )
    with pytest.raises(ForkSeedError, match="already taken"):
        prepare_fork_spawn("p", fork_name="taken-name", task="t")


def test_prepare_fork_spawn_derives_inline_doc(tmp_path, monkeypatch):
    import yaml

    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(_PARENT_DOC))
    monkeypatch.setattr(
        "scitex_agent_container._lifecycle._fork.resolve_config",
        lambda name: str(spec_path),
    )
    monkeypatch.setattr(
        "scitex_agent_container._lifecycle._fork.enumerate_agent_names",
        lambda: [],
    )
    monkeypatch.setattr(
        "scitex_agent_container._lifecycle._fork._resolve_parent_to_home",
        lambda path, doc: None,
    )
    name, doc = prepare_fork_spawn("p", task="green the audit")
    assert name == "p-fork"
    assert doc["spec"]["apptainer"]["env"]["SCITEX_CARDS_AGENT_ID"] == "p-fork"
    assert doc["spec"]["a2a"]["port"] == "auto"


def test_derived_doc_passes_validator_fork_surfaces():
    """End-to-end guard for the live-spawn failure: the derived doc must
    not trip the v3 validator on fork-touched surfaces (top-level env,
    legacy claude surface). Runs the REAL validator, not a shape
    assertion, so future validator drift fails here first."""
    from scitex_agent_container.config._validation import validate_raw

    doc = derive_fork_spec(
        _PARENT_DOC,
        fork_name="p-fork",
        parent_name="p",
        persist=False,
        task="green the audit",
    )
    errors = validate_raw(doc, "fork-verify/spec.yaml")
    fork_surface_errors = [
        e
        for e in errors
        if "no longer accepted at the top level" in e
        or "declare one surface" in e
    ]
    assert fork_surface_errors == []
