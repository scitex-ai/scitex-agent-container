"""Tests for the ``spec.a2a.host`` sweep and its post-write verification gate.

The gate is what makes the zero-behaviour-change claim checkable rather than
merely stated, so the cases that matter most are the ones where verification
FAILS and the whole sweep is undone — a partially-applied sweep is the one
outcome this must never produce.

STX-NM002: no mocks, no monkeypatch — real files, real apply, real rollback.
STX-TQ007: one logical assert per test.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._maintenance._a2a_host_sweep import (
    parse_specs,
    plan_a2a_host_sweep,
    verify_hosts,
)
from scitex_agent_container._maintenance._layers_migration_apply import apply_migration

_WITHOUT_HOST = "spec:\n  host: ywata-note-win\n  a2a:\n    port: auto\n"
_WITH_HOST = "spec:\n  a2a:\n    port: auto\n    host: 127.0.0.1\n"


def _write(root, agent: str, text: str):
    d = root / agent
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.yaml").write_text(text)
    return d / "spec.yaml"


@pytest.fixture()
def fleet(tmp_path):
    root = tmp_path / "agents"
    root.mkdir()
    _write(root, "needs-it", _WITHOUT_HOST)
    _write(root, "already-has-it", _WITH_HOST)
    yield root


def test_the_sweep_plans_the_one_spec_that_omits_the_key(fleet) -> None:
    # Arrange
    root = fleet
    # Act
    plan = plan_a2a_host_sweep(root)
    # Assert
    assert [e.agent for e in plan.writable] == ["needs-it"]


def test_applying_writes_the_declaration(fleet) -> None:
    # Arrange
    root = fleet
    plan = plan_a2a_host_sweep(root)
    before = parse_specs(plan)
    # Act
    apply_migration(plan, root / ".old" / "run", lambda: verify_hosts(plan, before))
    # Assert
    assert "    host: 127.0.0.1\n" in (root / "needs-it" / "spec.yaml").read_text()


def test_applying_reports_the_spec_it_wrote(fleet) -> None:
    # Arrange
    root = fleet
    plan = plan_a2a_host_sweep(root)
    before = parse_specs(plan)
    # Act
    result = apply_migration(
        plan, root / ".old" / "run", lambda: verify_hosts(plan, before)
    )
    # Assert
    assert result.written == ("needs-it",)


def test_the_originals_are_archived_before_the_write(fleet) -> None:
    # Arrange — the archive is the rollback path, not a courtesy copy.
    root = fleet
    plan = plan_a2a_host_sweep(root)
    before = parse_specs(plan)
    archive = root / ".old" / "run"
    # Act
    apply_migration(plan, archive, lambda: verify_hosts(plan, before))
    # Assert
    assert (archive / "needs-it__spec.yaml").read_text() == _WITHOUT_HOST


def test_the_archive_is_keyed_by_agent_not_filename(fleet) -> None:
    # Arrange — every file is called spec.yaml; a flat copy keyed by basename
    # would keep only one and invite a rollback that cannot complete.
    root = fleet
    _write(root, "second-one", _WITHOUT_HOST)
    plan = plan_a2a_host_sweep(root)
    before = parse_specs(plan)
    archive = root / ".old" / "run"
    # Act
    apply_migration(plan, archive, lambda: verify_hosts(plan, before))
    # Assert
    assert sorted(p.name for p in archive.iterdir()) == [
        "needs-it__spec.yaml",
        "second-one__spec.yaml",
    ]


def test_verification_passes_when_only_the_host_key_was_added(fleet) -> None:
    # Arrange
    root = fleet
    plan = plan_a2a_host_sweep(root)
    before = parse_specs(plan)
    apply_migration(plan, root / ".old" / "run", lambda: verify_hosts(plan, before))
    # Act
    diff = verify_hosts(plan, before)
    # Assert
    assert diff.safe is True


def test_a_wrong_host_fails_verification(fleet) -> None:
    # Arrange — ask the gate for a value the sweep did not write.
    root = fleet
    plan = plan_a2a_host_sweep(root)
    before = parse_specs(plan)
    apply_migration(plan, root / ".old" / "run", lambda: verify_hosts(plan, before))
    # Act
    diff = verify_hosts(plan, before, "0.0.0.0")
    # Assert
    assert diff.wrong == ("needs-it",)


def test_a_drifted_document_fails_verification(fleet) -> None:
    # Arrange — the host lands correctly but something ELSE moved; a per-file
    # "did my key arrive?" check is blind to exactly this.
    root = fleet
    plan = plan_a2a_host_sweep(root)
    before = parse_specs(plan)
    apply_migration(plan, root / ".old" / "run", lambda: verify_hosts(plan, before))
    (root / "needs-it" / "spec.yaml").write_text(
        "spec:\n  host: SOMEWHERE-ELSE\n  a2a:\n    port: auto\n    host: 127.0.0.1\n"
    )
    # Act
    diff = verify_hosts(plan, before)
    # Assert
    assert diff.drifted == ("needs-it",)


def test_an_unparsable_result_is_its_own_category(fleet) -> None:
    # Arrange — neither "fine" nor "wrong value"; different cause, different
    # fix, so it must not be folded into either.
    root = fleet
    plan = plan_a2a_host_sweep(root)
    before = parse_specs(plan)
    apply_migration(plan, root / ".old" / "run", lambda: verify_hosts(plan, before))
    (root / "needs-it" / "spec.yaml").write_text("spec: [unclosed\n")
    # Act
    diff = verify_hosts(plan, before)
    # Assert
    assert diff.unparsable == ("needs-it",)


def test_a_failing_gate_restores_the_original(fleet) -> None:
    # Arrange — the transactional promise: write, verify, UNDO.
    root = fleet
    plan = plan_a2a_host_sweep(root)
    # Act
    apply_migration(plan, root / ".old" / "run", lambda: verify_hosts(plan, {}, "nope"))
    # Assert
    assert (root / "needs-it" / "spec.yaml").read_text() == _WITHOUT_HOST


def test_a_failing_gate_reports_a_rollback_not_a_write(fleet) -> None:
    # Arrange — `refused` and `rolled_back` leave the same filesystem but say
    # very different things about what was learned.
    root = fleet
    plan = plan_a2a_host_sweep(root)
    # Act
    result = apply_migration(
        plan, root / ".old" / "run", lambda: verify_hosts(plan, {}, "nope")
    )
    # Assert
    assert result.rolled_back is not None


def test_an_already_migrated_fleet_plans_no_writes(fleet) -> None:
    # Arrange — re-running the sweep must be a no-op.
    root = fleet
    plan = plan_a2a_host_sweep(root)
    before = parse_specs(plan)
    apply_migration(plan, root / ".old" / "run", lambda: verify_hosts(plan, before))
    # Act
    again = plan_a2a_host_sweep(root)
    # Assert
    assert again.writable == ()


def test_the_already_declaring_spec_is_never_rewritten(fleet) -> None:
    # Arrange
    root = fleet
    plan = plan_a2a_host_sweep(root)
    before = parse_specs(plan)
    # Act
    apply_migration(plan, root / ".old" / "run", lambda: verify_hosts(plan, before))
    # Assert
    assert (root / "already-has-it" / "spec.yaml").read_text() == _WITH_HOST
