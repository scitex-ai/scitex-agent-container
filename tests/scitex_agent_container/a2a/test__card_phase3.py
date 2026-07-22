"""Phase-3 (ADR-0010 Step 2) — AgentCard isolation block surfaces the
per-spec ``lineage.may_spawn`` flag.

Mirrors ``test__card.py`` conventions: AAA, one assertion per test,
descriptive names. Pins both the explicit-false case (Gap-5 visible to
external verifiers) and the default-preservation case (absent block
keeps ``may_spawn=True``).
"""

from __future__ import annotations

from scitex_agent_container.a2a._card import project_card

_BASE_URL = "http://127.0.0.1:7901"


def _v3(extra_spec: dict | None = None) -> dict:
    spec = {"runtime": "apptainer", "claude": {"model": "sonnet"}}
    if extra_spec:
        spec.update(extra_spec)
    return {
        "apiVersion": "scitex-agent-container/v3",
        "metadata": {"labels": {"role": "worker"}},
        "spec": spec,
    }


def test_isolation_lineage_may_spawn_defaults_true_when_block_absent() -> None:
    """Default-preservation: spec without lineage block surfaces
    may_spawn=True in the card's isolation block."""
    # Arrange
    v3 = _v3()
    # Act
    card = project_card("cap-a", v3, _BASE_URL)
    # Assert
    assert card["x-scitex-agent-container"]["isolation"]["lineage"]["may_spawn"] is True


def test_isolation_lineage_may_spawn_false_surfaces_in_card() -> None:
    """Gap-5: spec.lineage.may_spawn=false is visible to external
    capsule attestation (Clew, fleet hubs) without parsing the YAML."""
    # Arrange
    v3 = _v3({"lineage": {"may_spawn": False}})
    # Act
    card = project_card("cap-a", v3, _BASE_URL)
    # Assert
    assert (
        card["x-scitex-agent-container"]["isolation"]["lineage"]["may_spawn"] is False
    )
