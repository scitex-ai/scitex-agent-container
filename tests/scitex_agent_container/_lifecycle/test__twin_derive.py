"""Twin spec derivation — the command-time half (``_lifecycle._twin_derive``).

Real behaviour, no mocks of the code under test: pure spec-doc transforms
tested against real dicts, and the derived document run through the REAL v3
validator (``test_derived_twin_spec_passes_v3_validation``) — the check the
original suite lacked, which let ``derive_twin_spec`` emit specs that could
not load.
"""

from __future__ import annotations

from scitex_agent_container._lifecycle._twin import (
    TWIN_PARENT_ENV,
    build_twin_boot_kick,
    derive_twin_spec,
)


def _parent_doc() -> dict:
    """A representative parent spec document (the raw v3 shape on disk).

    Env lives under ``spec.apptainer.env`` — the v3 home. A TOP-LEVEL
    ``spec.env`` (what this fixture used to carry) is REJECTED by the
    validator, so it modelled a spec that cannot exist on disk; see
    ``test_derived_twin_spec_passes_v3_validation``, which is the test that
    could have disagreed.

    ``raw_args`` mirrors the real fleet shape (e.g. the on-disk
    ``scitex-tex`` spec), which pins agent identity via ``--env`` there.
    """
    return {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "metadata": {"labels": {"role": "worker"}},
        "spec": {
            "runtime": "apptainer",
            # ``host: local`` is BANNED (operator directive 2026-07-10);
            # ${HOSTNAME} is the validator-documented portable form, as in
            # ``_write_parent_spec`` below.
            "host": "${HOSTNAME}",
            "workdir": "/home/agent/proj/x",
            # health.* / restart.max_retries are REQUIRED explicitly (no
            # hidden defaults; operator directive 2026-06-23).
            "health": {"enabled": True, "interval": 30, "method": "sdk-alive"},
            "apptainer": {
                "image": "/x.sif",
                "binds": ["~/proj:/home/agent/proj:rw"],
                "env": {
                    "SCITEX_TODO_AGENT_ID": "parent",
                    "SAC_NAME": "parent",
                    "FOO": "bar",
                },
                "raw_args": [
                    "--userns",
                    "--env",
                    "SCITEX_TODO_AGENT_ID=parent",
                    "--env",
                    "GIT_AUTHOR_NAME=Yusuke Watanabe",
                ],
            },
            "claude": {
                "model": "opus",
                "session": "continue",
                "channels": ["server:sac", "server:claude-code-telegrammer"],
            },
            "restart": {"policy": "always", "max_retries": 3},
            "a2a": {"port": 7901},
        },
    }


def _twin_env_block(out: dict) -> dict:
    """The derived twin's engine env block (``spec.apptainer.env``)."""
    return out["spec"]["apptainer"]["env"]

# ─── derive_twin_spec: identity split (safety-critical) ───────────────────


def test_derive_sets_todo_author_to_twin():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert _twin_env_block(out)["SCITEX_TODO_AGENT_ID"] == "parent-twin"


def test_derive_sets_twin_parent_env_to_parent():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert _twin_env_block(out)[TWIN_PARENT_ENV] == "parent"


def test_derive_drops_inherited_sac_name_env():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert "SAC_NAME" not in _twin_env_block(out)


def test_derive_inherits_other_env_verbatim():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert _twin_env_block(out)["FOO"] == "bar"


# ─── derive_twin_spec: session / lifetime / port / channels ───────────────


def test_derive_sets_session_continue():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert out["spec"]["claude"]["session"] == "continue"


def test_derive_clears_resume_id_for_host_resolution():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert out["spec"]["claude"]["resume_id"] == ""


def test_derive_ephemeral_sets_restart_never():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert out["spec"]["restart"]["policy"] == "never"


def test_derive_persist_sets_restart_always():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=True
    )
    # Assert
    assert out["spec"]["restart"]["policy"] == "always"


def test_derive_sets_fresh_a2a_port_auto():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert out["spec"]["a2a"]["port"] == "auto"


def test_derive_drops_telegrammer_channel():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert out["spec"]["claude"]["channels"] == ["server:sac"]


# ─── derive_twin_spec: inheritance / role / to_home / boot-kick ───────────


def test_derive_inherits_workdir_verbatim():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert out["spec"]["workdir"] == "/home/agent/proj/x"


def test_derive_inherits_image_verbatim():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert out["spec"]["apptainer"]["image"] == "/x.sif"


def test_derive_sets_role_label_when_given():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="t", parent_name="parent", persist=False, role="writer"
    )
    # Assert
    assert out["metadata"]["labels"]["role"] == "writer"


def test_derive_sets_to_home_when_given():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="t", parent_name="parent", persist=False, to_home="/abs/th"
    )
    # Assert
    assert out["spec"]["to_home"] == "/abs/th"


def test_derive_startup_prompt_carries_ownership_rule():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert "assignee=parent" in out["spec"]["startup_prompts"][0]


def test_derive_does_not_mutate_parent_doc():
    # Arrange
    doc = _parent_doc()
    # Act
    derive_twin_spec(doc, twin_name="parent-twin", parent_name="parent", persist=False)
    # Assert
    assert doc["spec"]["apptainer"]["env"]["SCITEX_TODO_AGENT_ID"] == "parent"


# ─── build_twin_boot_kick ─────────────────────────────────────────────────


def test_boot_kick_states_owner_stays_parent():
    # Arrange
    parent = "neurovista"
    # Act
    kick = build_twin_boot_kick("neurovista-twin", parent, None)
    # Assert
    assert "assignee=neurovista" in kick


def test_boot_kick_includes_task_when_given():
    # Arrange
    task = "audit the failing figures"
    # Act
    kick = build_twin_boot_kick("t", "p", task)
    # Assert
    assert task in kick


# ─── the derived spec must actually LOAD (the test that could disagree) ────


def test_derived_twin_spec_passes_v3_validation():
    # Arrange — the REAL validator, not a hand-read of the shape. The old
    # fixture put env at the top level, which validate_raw REJECTS, so the
    # suite asserted on a spec that could never exist on disk and the twin
    # spawned unloadable specs in production.
    from scitex_agent_container.config._validation import validate_raw

    doc = derive_twin_spec(
        _parent_doc(), twin_name="parent-forked-t", parent_name="parent", persist=False
    )
    # Act
    errors = validate_raw(doc, "agents/parent-forked-t/spec.yaml")
    # Assert
    assert errors == []


def test_derive_puts_identity_env_where_the_loader_reads_it():
    # Arrange — spec.apptainer.env is the v3 home AND the only block that
    # reaches both config.env and the container's --env.
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-forked-t", parent_name="parent", persist=False
    )
    # Assert
    assert "env" not in out["spec"]


def test_derive_scrubs_parents_identity_from_inherited_raw_args():
    # Arrange — raw_args are appended AFTER the curated --env, so an
    # inherited `--env SCITEX_TODO_AGENT_ID=<parent>` would re-assert the
    # parent's identity inside the twin's container.
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-forked-t", parent_name="parent", persist=False
    )
    # Assert
    assert "SCITEX_TODO_AGENT_ID=parent" not in out["spec"]["apptainer"]["raw_args"]


def test_derive_keeps_unrelated_raw_args_verbatim():
    # Arrange — only IDENTITY --env pairs are scrubbed; ADR-0019 requires
    # everything else inherited verbatim.
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-forked-t", parent_name="parent", persist=False
    )
    raw = out["spec"]["apptainer"]["raw_args"]
    # Assert
    assert raw == ["--userns", "--env", "GIT_AUTHOR_NAME=Yusuke Watanabe"]
