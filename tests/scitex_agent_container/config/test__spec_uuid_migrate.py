"""Moving working-tree-only ``spec.uuid`` stamps into runtime state.

SAC < 0.40 stamped ``uuid:`` into specs at start; those lines are often
absent from the committed file. The migration records the uuid where the
start path reads it (``<state_dir>/spec_uuid``) and removes the line, so the
identity survives. Real files, real ``git`` repos, real state-dir
resolution (project-scope ``.scitex/agent-container`` inside a tmp repo).
AAA markers, one assertion per test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from scitex_agent_container.config._spec_uuid import (
    ensure_spec_uuid,
    runtime_spec_uuid,
)
from scitex_agent_container.config._spec_uuid_migrate import (
    default_state_dir_for_spec,
    migrate_spec_uuids,
)
from tests.scitex_agent_container.config.test__explicit_validation import (
    _explicit_doc,
    _write_doc,
)

_OTHER = "0f0e0d0c-0b0a-4908-8706-050403020100"
_AUTHORED = "12345678-1234-5678-1234-567812345678"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(repo),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def make_fleet(tmp_path: Path, *, name: str = "agent-a", uuid: str = "") -> Path:
    """A git repo holding a project-scope agents dir with one committed spec.

    Returns the spec path. The state dir the START path resolves for it is
    ``<repo>/.scitex/agent-container/runtime/<name>``.
    """
    repo = tmp_path / "repo"
    agents = repo / ".scitex" / "agent-container" / "agents"
    agents.mkdir(parents=True)
    _git(repo, "init", "-q")
    doc = _explicit_doc()
    if uuid:
        doc["spec"]["uuid"] = uuid
    spec = _write_doc(agents, doc, name=name)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "spec")
    return spec


def _stamped_fleet(tmp_path: Path) -> "tuple[Path, str]":
    """A committed uuid-less spec plus a working-tree-only stamp (SAC < 0.40)."""
    spec = make_fleet(tmp_path)
    return spec, ensure_spec_uuid(spec)


def _state_dir(spec: Path) -> Path:
    return spec.parents[2] / "runtime" / spec.parent.name


def test_start_state_dir_is_project_scope(tmp_path: Path) -> None:
    # Arrange — guards every test below against writing into the real home.
    spec = make_fleet(tmp_path)
    # Act
    got = default_state_dir_for_spec(spec)
    # Assert
    assert got == _state_dir(spec).resolve()


def test_stamp_moves_into_runtime_state(tmp_path: Path) -> None:
    # Arrange
    spec, stamped = _stamped_fleet(tmp_path)
    migrate_spec_uuids(spec.parents[1])
    # Act — what the next start of the now uuid-less spec will use.
    got = runtime_spec_uuid(default_state_dir_for_spec(spec))
    # Assert
    assert got == stamped


def test_stamp_line_is_removed_leaving_the_checkout_clean(tmp_path: Path) -> None:
    # Arrange
    spec, _ = _stamped_fleet(tmp_path)
    # Act
    migrate_spec_uuids(spec.parents[1])
    # Assert
    assert _git(spec.parent, "status", "--porcelain", "--", "spec.yaml") == ""


def test_stamp_reports_migrated(tmp_path: Path) -> None:
    # Arrange
    spec, _ = _stamped_fleet(tmp_path)
    # Act
    (result,) = migrate_spec_uuids(spec.parents[1])
    # Assert
    assert result.action == "migrated"


def test_authored_uuid_is_left_untouched(tmp_path: Path) -> None:
    # Arrange
    spec = make_fleet(tmp_path, uuid=_AUTHORED)
    before = spec.read_bytes()
    # Act
    migrate_spec_uuids(spec.parents[1])
    # Assert
    assert spec.read_bytes() == before


def test_authored_uuid_reports_authored(tmp_path: Path) -> None:
    # Arrange
    spec = make_fleet(tmp_path, uuid=_AUTHORED)
    # Act
    (result,) = migrate_spec_uuids(spec.parents[1])
    # Assert
    assert result.action == "authored"


def test_conflicting_state_is_refused(tmp_path: Path) -> None:
    # Arrange
    spec, _ = _stamped_fleet(tmp_path)
    _state_dir(spec).mkdir(parents=True)
    (_state_dir(spec) / "spec_uuid").write_text(_OTHER + "\n")
    # Act
    (result,) = migrate_spec_uuids(spec.parents[1])
    # Assert
    assert result.action == "refused"


def test_conflict_leaves_spec_and_state_unchanged(tmp_path: Path) -> None:
    # Arrange
    spec, _ = _stamped_fleet(tmp_path)
    _state_dir(spec).mkdir(parents=True)
    (_state_dir(spec) / "spec_uuid").write_text(_OTHER + "\n")
    before = (spec.read_bytes(), (_state_dir(spec) / "spec_uuid").read_bytes())
    # Act
    migrate_spec_uuids(spec.parents[1])
    # Assert
    assert (spec.read_bytes(), (_state_dir(spec) / "spec_uuid").read_bytes()) == before


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    # Arrange
    spec, _ = _stamped_fleet(tmp_path)
    before = spec.read_bytes()
    # Act
    migrate_spec_uuids(spec.parents[1], dry_run=True)
    # Assert
    assert (spec.read_bytes(), _state_dir(spec).exists()) == (before, False)


def test_dry_run_reports_the_plan(tmp_path: Path) -> None:
    # Arrange
    spec, stamped = _stamped_fleet(tmp_path)
    # Act
    (result,) = migrate_spec_uuids(spec.parents[1], dry_run=True)
    # Assert
    assert (result.action, result.uuid, result.line_removed) == (
        "migrated",
        stamped,
        True,
    )


def test_rerun_after_migration_is_a_no_op(tmp_path: Path) -> None:
    # Arrange
    spec, _ = _stamped_fleet(tmp_path)
    migrate_spec_uuids(spec.parents[1])
    # Act
    (result,) = migrate_spec_uuids(spec.parents[1])
    # Assert
    assert result.action == "no-uuid"


def test_single_agent_name_limits_the_run(tmp_path: Path) -> None:
    # Arrange
    spec, _ = _stamped_fleet(tmp_path)
    _write_doc(spec.parents[1], _explicit_doc(), name="agent-b")
    # Act
    results = migrate_spec_uuids(spec.parents[1], name="agent-a")
    # Assert
    assert [r.agent for r in results] == ["agent-a"]


def test_outside_git_persists_state_but_keeps_line(tmp_path: Path) -> None:
    # Arrange — no repository: there is no committed version to compare.
    agents = tmp_path / "agents"
    spec = _write_doc(agents, _explicit_doc(), name="agent-a")
    stamped = ensure_spec_uuid(spec)
    before = spec.read_bytes()
    state = tmp_path / "state"
    # Act
    (result,) = migrate_spec_uuids(agents, state_dir_for=lambda _p: state)
    # Assert
    assert (result.action, spec.read_bytes(), runtime_spec_uuid(state)) == (
        "persisted",
        before,
        stamped,
    )
