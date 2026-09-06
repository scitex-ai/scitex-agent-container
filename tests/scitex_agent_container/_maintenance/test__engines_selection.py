"""The SELECTION must add up to the spec.yaml files on disk.

Real spec files under ``tmp_path``, selected and planned by the real
functions. No mocks: every fact here is about a file the sweep did NOT look
at, and a mocked selector would report only what the test author believed.

THE FOUR WAYS A SPEC USED TO LEAVE THE COUNT UNNAMED:

1. **A shadowed copy.** Two roots hold a spec.yaml for the same agent name;
   the de-duplication keeps one and dropped the other into no bucket and no
   payload field. Measured: 4 spec.yaml on disk, ``specs: 3``. Earlier-root-
   wins is deterministic, so no later run reaches it either — and the apply
   still reported ``migration_complete``.
2. **An unmigrated template.** ``sac agents create`` copies those, so a sweep
   that leaves one behind re-introduces the legacy shape on every agent minted
   afterwards. The prose said so; the one boolean built for a machine reader
   did not.
3. **A ``--agent`` value that matched nothing.** Set membership discards a
   typo or a renamed agent in silence.
4. **A ``--host`` value that matched nothing.** Same.

STX-NM002: no mocks. STX-TQ002 / TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scitex_agent_container._maintenance._engines_floor import EngineFloor
from scitex_agent_container._maintenance._engines_migration import (
    plan_engines_migration,
    select_spec_paths,
    select_spec_paths_over_roots,
)
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc

#: Measured engines-capable, so the version floor is not what these tests are
#: about — the selection is.
CAPABLE = "scitex-compute-04"
_NO_FLOOR = EngineFloor.disabled()


def _write_spec(root: Path, name: str, *, host: str = CAPABLE) -> Path:
    agent_dir = root / name
    agent_dir.mkdir(parents=True)
    doc = explicit_doc(
        {"to_home": "./to_home", "claude": {"model": "opus[1m]"}, "host": host}
    )
    path = agent_dir / "spec.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def _migrate_in_place(paths) -> None:
    """Write the migrated text back, so those specs read as already-migrated."""
    plan = plan_engines_migration(list(paths), floor=_NO_FLOOR)
    for outcome in plan.migrated:
        outcome.path.write_text(outcome.new_text)


def _declares_engines(path: Path) -> bool:
    return "engines" in yaml.safe_load(path.read_text())["spec"]


@pytest.fixture
def two_roots(tmp_path: Path):
    first = tmp_path / "rootA"
    second = tmp_path / "rootB"
    first.mkdir()
    second.mkdir()
    return first, second


# ---------------------------------------------------------------------------
# 1. A shadowed copy is a real file, and it is named
# ---------------------------------------------------------------------------


def test_the_shadowed_copy_is_named_with_its_path(two_roots) -> None:
    # Arrange — the loser of the de-duplication used to appear nowhere.
    first, second = two_roots
    _write_spec(first, "dup")
    dropped = _write_spec(second, "dup")
    # Act
    selection = select_spec_paths_over_roots((first, second))
    # Assert
    assert [s.dropped for s in selection.shadowed] == [dropped]


def test_the_shadowed_entry_names_the_copy_that_was_kept(two_roots) -> None:
    # Arrange — resolving the collision means looking at BOTH files.
    first, second = two_roots
    kept = _write_spec(first, "dup")
    _write_spec(second, "dup")
    # Act
    selection = select_spec_paths_over_roots((first, second))
    # Assert
    assert selection.shadowed[0].kept == kept


def test_the_selection_accounts_for_every_spec_on_disk(two_roots) -> None:
    # Arrange — the measured shape: 4 spec.yaml, and the plan said 3.
    first, second = two_roots
    for root, names in ((first, ("dup", "onlyA")), (second, ("dup", "onlyB"))):
        for name in names:
            _write_spec(root, name)
    # Act
    selection = select_spec_paths_over_roots((first, second))
    # Assert
    assert len(selection.paths) + len(selection.shadowed) == 4


def test_a_shadowed_copy_keeps_the_plan_from_claiming_completion(
    two_roots,
) -> None:
    # Arrange — every SELECTED spec is already migrated, and rootB still holds
    # a legacy spec.yaml no run of this sweep can ever reach.
    first, second = two_roots
    _write_spec(first, "dup")
    _write_spec(second, "dup")
    selection = select_spec_paths_over_roots((first, second))
    _migrate_in_place(selection.paths)
    # Act
    plan = plan_engines_migration(
        list(selection.paths),
        roots=(first, second),
        shadowed=selection.shadowed,
        floor=_NO_FLOOR,
    )
    # Assert
    assert plan.is_complete is False


def test_the_shadowed_copy_really_is_still_legacy(two_roots) -> None:
    # Arrange — the positive control for the claim above: the file the sweep
    # skipped is a real spec that really does still carry the old shape.
    first, second = two_roots
    _write_spec(first, "dup")
    dropped = _write_spec(second, "dup")
    selection = select_spec_paths_over_roots((first, second))
    # Act
    _migrate_in_place(selection.paths)
    # Assert
    assert _declares_engines(dropped) is False


def test_the_kept_copy_really_was_migrated(two_roots) -> None:
    # Arrange — the other half of the control: exactly one of the two moved.
    first, second = two_roots
    kept = _write_spec(first, "dup")
    _write_spec(second, "dup")
    selection = select_spec_paths_over_roots((first, second))
    # Act
    _migrate_in_place(selection.paths)
    # Assert
    assert _declares_engines(kept) is True


def test_one_root_alone_shadows_nothing(two_roots) -> None:
    # Arrange — the control: de-duplication must not invent a collision.
    first, _ = two_roots
    _write_spec(first, "alpha")
    _write_spec(first, "beta")
    # Act
    selection = select_spec_paths_over_roots((first,))
    # Assert
    assert selection.shadowed == ()


# ---------------------------------------------------------------------------
# 2. A skipped template is work left behind
# ---------------------------------------------------------------------------


def test_a_skipped_template_keeps_the_plan_from_claiming_completion(
    tmp_path: Path,
) -> None:
    # Arrange — the real agent is migrated; the template `agents create`
    # copies is not, so every agent minted afterwards is legacy again.
    root = tmp_path / "agents"
    root.mkdir()
    _write_spec(root, "real-agent")
    _write_spec(root, "_template_generalist")
    selection = select_spec_paths_over_roots((root,))
    _migrate_in_place(selection.paths)
    # Act
    plan = plan_engines_migration(
        list(selection.paths),
        roots=(root,),
        skipped_templates=list(selection.skipped_templates),
        floor=_NO_FLOOR,
    )
    # Assert
    assert plan.is_complete is False


def test_the_skipped_template_really_is_still_legacy(tmp_path: Path) -> None:
    # Arrange — the positive control for the claim above.
    root = tmp_path / "agents"
    root.mkdir()
    _write_spec(root, "real-agent")
    template = _write_spec(root, "_template_generalist")
    selection = select_spec_paths_over_roots((root,))
    # Act
    _migrate_in_place(selection.paths)
    # Assert
    assert _declares_engines(template) is False


def test_including_the_templates_lets_the_plan_claim_completion(
    tmp_path: Path,
) -> None:
    # Arrange — the positive control: --templates leaves nothing behind, so
    # the claim must still be makeable.
    root = tmp_path / "agents"
    root.mkdir()
    _write_spec(root, "real-agent")
    _write_spec(root, "_template_generalist")
    selection = select_spec_paths_over_roots((root,), templates=True)
    _migrate_in_place(selection.paths)
    # Act
    plan = plan_engines_migration(
        list(selection.paths),
        roots=(root,),
        skipped_templates=list(selection.skipped_templates),
        floor=_NO_FLOOR,
    )
    # Assert
    assert plan.is_complete is True


# ---------------------------------------------------------------------------
# 3 & 4. A selector that matched nothing
# ---------------------------------------------------------------------------


def test_an_agent_selector_matching_nothing_is_named(tmp_path: Path) -> None:
    # Arrange — a scripted `-a a -a b -a c` loop where `c` was renamed covers
    # two of three on every run and never says which one it could not find.
    root = tmp_path / "agents"
    root.mkdir()
    _write_spec(root, "business")
    # Act
    selection = select_spec_paths_over_roots(
        (root,), agents=("business", "NOSUCH-AGENT-TYPO")
    )
    # Assert
    assert selection.unmatched_agents == ("NOSUCH-AGENT-TYPO",)


def test_an_agent_selector_that_matched_is_not_named(tmp_path: Path) -> None:
    # Arrange — the control: a selector that worked must not be reported.
    root = tmp_path / "agents"
    root.mkdir()
    _write_spec(root, "business")
    # Act
    selection = select_spec_paths_over_roots((root,), agents=("business",))
    # Assert
    assert selection.unmatched_agents == ()


def test_a_partial_agent_miss_still_selects_the_ones_that_matched(
    tmp_path: Path,
) -> None:
    # Arrange — reporting the miss must not cost the hits.
    root = tmp_path / "agents"
    root.mkdir()
    _write_spec(root, "business")
    # Act
    selection = select_spec_paths_over_roots(
        (root,), agents=("business", "NOSUCH-AGENT-TYPO")
    )
    # Assert
    assert [p.parent.name for p in selection.paths] == ["business"]


def test_a_host_selector_matching_nothing_is_named(tmp_path: Path) -> None:
    # Arrange
    root = tmp_path / "agents"
    root.mkdir()
    _write_spec(root, "here", host="scitex-compute-01")
    # Act
    selection = select_spec_paths_over_roots(
        (root,), hosts=("scitex-compute-01", "NOSUCHHOST")
    )
    # Assert
    assert selection.unmatched_hosts == ("NOSUCHHOST",)


def test_a_host_selector_that_matched_is_not_named(tmp_path: Path) -> None:
    # Arrange — the control.
    root = tmp_path / "agents"
    root.mkdir()
    _write_spec(root, "here", host="scitex-compute-01")
    # Act
    selection = select_spec_paths_over_roots((root,), hosts=("scitex-compute-01",))
    # Assert
    assert selection.unmatched_hosts == ()


def test_an_unreadable_spec_suppresses_the_unmatched_host_claim(
    tmp_path: Path,
) -> None:
    # Arrange — "no spec declares that host" is a CLAIM, and a spec whose
    # hosts could not be read is not evidence for it. The unreadable spec is
    # reported in its own bucket instead.
    root = tmp_path / "agents"
    root.mkdir()
    broken = _write_spec(root, "broken", host="scitex-compute-01")
    broken.chmod(0o000)
    # Act
    try:
        selection = select_spec_paths_over_roots((root,), hosts=("NOSUCHHOST",))
    finally:
        broken.chmod(0o644)
    # Assert
    assert selection.unmatched_hosts == ()


def test_the_single_root_helper_still_returns_paths_and_templates(
    tmp_path: Path,
) -> None:
    # Arrange — the bucket-level unit tests unpack a 2-tuple from this, and a
    # single root cannot shadow anything, so the shorthand keeps its shape.
    root = tmp_path / "agents"
    root.mkdir()
    _write_spec(root, "alpha")
    _write_spec(root, "_template_handyman")
    # Act
    paths, skipped = select_spec_paths(root)
    # Assert
    assert ([p.parent.name for p in paths], skipped) == (
        ["alpha"],
        ["_template_handyman"],
    )
