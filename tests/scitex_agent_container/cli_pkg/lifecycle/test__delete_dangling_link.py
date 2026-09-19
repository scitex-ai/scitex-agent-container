"""A dangling spec link is the debris half-done deletions leave, and it is the
one case 'sac agents delete' used to skip.

The existence test was ``spec_dir.exists()``, which FOLLOWS a symlink, so an
entry whose authority target had been deleted reported "not found (no spec,
runtime, or registry)" and was skipped — measured on compute-03 2026-09-19 for
scitex-hub_pr943_integrator and _reviewer, whose cleanup then had no sanctioned
verb at all.

These tests pin the fixed behaviour and need NO database: the registry and the
peer lookup are stubbed, because the dangling path must not depend on either.
"""

from __future__ import annotations

import pathlib

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.lifecycle import _delete


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """A private HOME with an agents root, so nothing touches the real one."""
    home = tmp_path / "home"
    (home / ".scitex" / "agent-container" / "agents").mkdir(parents=True)
    (home / ".scitex" / "agent-container" / "runtime").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_delete, "is_in_sif", lambda: False, raising=False)
    monkeypatch.setattr(_delete, "lookup_remote_peer", lambda name: None)
    return home


class _NoRegistry:
    """Stands in for the DB-backed registry: no agent is registered."""

    def exists(self, name: str) -> bool:  # noqa: ARG002
        return False

    def remove(self, name: str) -> None:  # noqa: ARG002
        return None


@pytest.fixture()
def no_registry(monkeypatch):
    monkeypatch.setattr(_delete, "Registry", _NoRegistry)


def _dangling_link(home, name: str) -> pathlib.Path:
    agents = home / ".scitex" / "agent-container" / "agents"
    target = agents / "gone-elsewhere" / name  # never created: this is the point
    link = agents / name
    link.symlink_to(target, target_is_directory=True)
    assert link.is_symlink(), "fixture must produce a symlink"
    assert not link.exists(), "fixture must produce a DANGLING symlink"
    return link


def test_dry_run_reports_dangling_rather_than_not_found(fake_home, no_registry):
    link = _dangling_link(fake_home, "pr943_integrator")
    result = CliRunner().invoke(_delete.delete, ["pr943_integrator", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "DANGLING" in result.output
    assert "not found" not in result.output
    assert link.is_symlink(), "a dry run must not remove anything"


def test_delete_removes_the_dangling_link(fake_home, no_registry):
    link = _dangling_link(fake_home, "pr943_reviewer")
    result = CliRunner().invoke(_delete.delete, ["pr943_reviewer"])
    assert result.exit_code == 0, result.output
    assert not link.is_symlink() and not link.exists(), "the link must be gone"
    assert "not found" not in result.output


def test_a_genuinely_absent_name_is_still_reported_not_found(fake_home, no_registry):
    """The fix must not make the verb claim to have deleted nothing."""
    result = CliRunner().invoke(_delete.delete, ["never-existed-anywhere"])
    assert "not found" in result.output
    assert result.exit_code == 1, "nothing found is still a non-zero outcome"
