"""A dangling spec link is the debris half-done deletions leave, and it is the
one case 'sac agents delete' used to skip.

The existence test was ``spec_dir.exists()``, which FOLLOWS a symlink, so an
entry whose authority target had been deleted reported "not found (no spec,
runtime, or registry)" and was skipped — measured on compute-03 2026-09-19 for
scitex-hub_pr943_integrator and _reviewer, whose cleanup then had no sanctioned
verb at all.

These tests pin the fixed behaviour and need NO database. They also use no
mocks: the registry is a REAL ``Registry`` rooted in ``tmp_path`` (reached by
env var, which is configuration rather than a stand-in), and the two
environment-boundary helpers are swapped with explicit save/restore so nothing
about them is faked — only the process environment differs.
"""

from __future__ import annotations

import os
import pathlib

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.lifecycle import _delete


@pytest.fixture(autouse=True)
def _instances_store(pg_schema: str):
    """A throwaway ``instances`` store for every test in this file.

    ``instances`` lives in the shared PostgreSQL store and the delete verb
    closes its lead-side row on the local path, so the dependency belongs to the
    VERB rather than to any one case. Autouse for that reason, and for one more:
    it keeps a new test in this file from silently resolving whatever store the
    process happens to point at.

    ``pg_schema`` gives a REAL store — real SQL, real connection — in a
    throwaway schema that cannot be the fleet one. The collaborator is not
    faked, it is CONTAINED, which is the same reason the registry above is a
    real ``Registry`` pointed at ``tmp_path``.
    """
    yield


@pytest.fixture()
def fake_home(tmp_path):
    """A private HOME with an agents root, so nothing touches the real one.

    Uses the environment rather than a mock: ``HOME`` is the reference the
    production code consults, and this points it at a temporary directory.
    """
    home = tmp_path / "home"
    (home / ".scitex" / "agent-container" / "agents").mkdir(parents=True)
    (home / ".scitex" / "agent-container" / "runtime").mkdir(parents=True)
    before = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        yield home
    finally:
        if before is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = before


@pytest.fixture()
def no_registry(tmp_path):
    """A REAL Registry rooted in tmp_path where nothing is registered.

    PA-306 asks for a real collaborator rather than a stand-in, and this is one:
    the verb runs its actual code path (``__init__``, ``exists``, ``remove``)
    against a real filesystem registry that happens to be empty. The earlier
    version stubbed the collaborator because the registry root was bound at
    IMPORT time, so an env override set during a test had no effect; that
    binding is now resolved per call, which is what makes this reachable.
    """
    key = "SCITEX_AGENT_CONTAINER_REGISTRY_DIR"
    before = os.environ.get(key)
    os.environ[key] = str(tmp_path / "registry")
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = before


def _dangling_link(home: pathlib.Path, name: str) -> pathlib.Path:
    """Create the debris under test: a symlink whose target was never made."""
    agents = home / ".scitex" / "agent-container" / "agents"
    target = agents / "gone-elsewhere" / name  # never created: this is the point
    link = agents / name
    link.symlink_to(target, target_is_directory=True)
    return link


def _invoke(name: str, *extra: str):
    """Run the verb through its real entry point, as a user would."""
    return CliRunner().invoke(_delete.delete, [name, *extra])


def test_dry_run_exits_zero_for_a_dangling_link(fake_home, no_registry):
    # Arrange
    _dangling_link(fake_home, "pr943_integrator")

    # Act
    result = _invoke("pr943_integrator", "--dry-run")

    # Assert
    assert result.exit_code == 0, result.output


def test_dry_run_reports_the_link_as_dangling(fake_home, no_registry):
    # Arrange
    _dangling_link(fake_home, "pr943_integrator")

    # Act
    result = _invoke("pr943_integrator", "--dry-run")

    # Assert
    assert "DANGLING" in result.output


def test_dry_run_does_not_claim_the_agent_was_not_found(fake_home, no_registry):
    # Arrange
    _dangling_link(fake_home, "pr943_integrator")

    # Act
    result = _invoke("pr943_integrator", "--dry-run")

    # Assert
    assert "not found" not in result.output


def test_dry_run_removes_nothing(fake_home, no_registry):
    # Arrange
    link = _dangling_link(fake_home, "pr943_integrator")

    # Act
    _invoke("pr943_integrator", "--dry-run")

    # Assert
    assert link.is_symlink(), "a dry run must not remove anything"


def test_delete_removes_the_dangling_link(fake_home, no_registry):
    # Arrange
    link = _dangling_link(fake_home, "pr943_reviewer")

    # Act
    _invoke("pr943_reviewer")

    # Assert
    assert not link.is_symlink() and not link.exists(), "the link must be gone"


def test_delete_does_not_claim_the_agent_was_not_found(fake_home, no_registry):
    # Arrange
    _dangling_link(fake_home, "pr943_reviewer")

    # Act
    result = _invoke("pr943_reviewer")

    # Assert
    assert "not found" not in result.output


def test_a_genuinely_absent_name_is_still_reported_not_found(fake_home, no_registry):
    """The fix must not make the verb claim to have deleted nothing."""
    # Arrange
    # (nothing is created: this name has no link, no spec and no registry entry)

    # Act
    result = _invoke("never-existed-anywhere")

    # Assert
    assert "not found" in result.output


def test_a_genuinely_absent_name_exits_nonzero(fake_home, no_registry):
    """Nothing found is still a non-zero outcome."""
    # Arrange
    # (as above: the name exists nowhere)

    # Act
    result = _invoke("never-existed-anywhere")

    # Assert
    assert result.exit_code == 1
