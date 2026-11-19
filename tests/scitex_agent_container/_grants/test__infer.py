"""``_grants._infer`` — infer the legacy-equivalent block, then prove it.

Uses the ``world`` fixture (``conftest.py``): a fake host holding every
grantable source and one Claude-TUI spec. No mocks.
"""

from __future__ import annotations

from scitex_agent_container._grants._infer import check, infer, load_variant
from scitex_agent_container.config._grants import all_granted
from tests.scitex_agent_container._helpers.grants_harness import harness_variant


def test_refused_legacy_render_infers_complete_block(world) -> None:
    # Arrange — a Hermes spec with no AGENTS.md projection: the legacy render
    # itself is refused, so nothing past the refusal is observable.
    raw, spec, root = harness_variant(world, "hermes-tui", agents_md=False)
    # Act
    block, _legacy, _volatile = infer(raw, spec, root=root)
    # Assert — conservative: the complete block (host_exec follows groups).
    assert {k: v for k, v in block.items() if k != "host_exec"} == {
        k: v for k, v in all_granted().to_block().items() if k != "host_exec"
    }


def test_refused_legacy_render_is_reported_inconclusive(world) -> None:
    # Arrange
    raw, spec, root = harness_variant(world, "hermes-tui", agents_md=False)
    # Act
    result = check(raw, spec, root=root)
    # Assert
    assert (result.conclusive, result.passed) == (False, False)


def test_inferred_block_reproduces_legacy_surface(world) -> None:
    # Arrange
    raw, spec, root = world
    # Act
    result = check(raw, spec, root=root)
    # Assert
    assert (result.passed, result.diff) == (True, [])


def test_inferred_block_names_present_fleet_binds(world) -> None:
    # Arrange
    raw, spec, root = world
    # Act
    block, _legacy, _volatile = infer(raw, spec, root=root)
    # Assert
    assert {"cards", "cct_store", "host_tmp", "quota_cache"} <= set(block["host_binds"])


def test_inferred_block_withholds_unused_host_merge(world) -> None:
    # Arrange — the world's agent is not a full developer.
    raw, spec, root = world
    # Act
    block, _legacy, _volatile = infer(raw, spec, root=root)
    # Assert
    assert "merge" not in block["host_claude"]


def test_inferred_block_withholds_host_exec_without_group(world) -> None:
    # Arrange — no eligible group in the world's labels.
    raw, spec, root = world
    # Act
    block, _legacy, _volatile = infer(raw, spec, root=root)
    # Assert
    assert block["host_exec"] is False


def test_invalid_variant_is_rejected_by_validation(world) -> None:
    # Arrange
    raw, spec, _root = world
    # Act
    try:
        load_variant(raw, spec, {"host_exec": True})
        message = ""
    except ValueError as exc:
        message = str(exc)
    # Assert
    assert "requires spec.grants.sac_control" in message
