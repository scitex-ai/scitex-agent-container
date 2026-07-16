"""Tests for ``_maintenance._worktree_gc_repos`` — what ``--all`` sweeps.

PA-306: no ``unittest.mock``. Real temp spec trees (real ``spec.yaml``
files) and real git repos; ``roots`` is the injected seam so the suite
never reads the operator's actual agent tree.

The behaviours that matter — every one of them is a way ``--all`` could
sweep something it should not:

* a spec's ``spec.workdir`` that IS a local git repo toplevel -> swept,
* a workdir on another host / in a container -> skipped (not a dir here),
* a workdir that is not a repo -> skipped,
* a workdir merely INSIDE a repo -> skipped (never drag the parent in),
* two specs sharing a repo -> swept once,
* a malformed spec -> skipped, and the other specs still discovered.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scitex_agent_container._maintenance._worktree_gc_repos import (
    discover_repos,
    spec_workdirs,
)


def _write_spec(root: Path, name: str, workdir: str | None) -> None:
    """A real spec.yaml in the real on-disk shape sac uses."""
    agent = root / name
    agent.mkdir(parents=True)
    body = [
        "apiVersion: scitex-agent-container/v3",
        "kind: Agent",
        f"metadata:\n  labels:\n    project: {name}",
        "spec:",
        "  runtime: tui",
    ]
    if workdir is not None:
        body.append(f"  workdir: {workdir}")
    (agent / "spec.yaml").write_text("\n".join(body) + "\n")


@pytest.fixture
def spec_root(tmp_path: Path) -> Path:
    root = tmp_path / "agents"
    root.mkdir()
    return root


@pytest.fixture
def real_repo(tmp_path: Path) -> Path:
    """A real git repo — `git init` for real, no fixture theatre."""
    repo = tmp_path / "a-real-repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "develop"], check=True)
    return repo


def test_spec_workdir_pointing_at_a_repo_is_discovered(spec_root, real_repo):
    # Arrange — the canonical case: a maintainer agent whose workdir IS
    # the repo it maintains.
    _write_spec(spec_root, "maintainer", str(real_repo))
    # Act
    found = discover_repos([spec_root])
    # Assert
    assert found == [str(real_repo)]


def test_workdir_on_another_host_is_skipped(spec_root):
    # Arrange — specs describe agents on Spartan and inside containers
    # too. This GC only ever touches the machine it runs on.
    _write_spec(spec_root, "remote", "/data/gpfs/projects/punim0264/nope")
    # Act
    found = discover_repos([spec_root])
    # Assert
    assert found == []


def test_workdir_that_is_not_a_repo_is_skipped(spec_root, tmp_path):
    # Arrange — the DEFAULT per-agent workdir is a runtime workspace, not
    # a repo. It has no worktrees and must never be swept.
    plain = tmp_path / "plain-workspace"
    plain.mkdir()
    _write_spec(spec_root, "plain", str(plain))
    # Act
    found = discover_repos([spec_root])
    # Assert
    assert found == []


def test_workdir_nested_inside_a_repo_is_skipped(spec_root, real_repo):
    # Arrange — an agent working in a SUBDIRECTORY of a repo must not drag
    # the whole enclosing repo into the sweep: that would silently widen
    # the blast radius past what the spec author declared.
    nested = real_repo / "subproject"
    nested.mkdir()
    _write_spec(spec_root, "nested", str(nested))
    # Act
    found = discover_repos([spec_root])
    # Assert
    assert found == []


def test_two_specs_sharing_a_repo_yield_one(spec_root, real_repo):
    # Arrange — many agents can point at one repo; sweeping it twice would
    # double every removal attempt.
    _write_spec(spec_root, "agent-one", str(real_repo))
    _write_spec(spec_root, "agent-two", str(real_repo))
    # Act
    found = discover_repos([spec_root])
    # Assert
    assert found == [str(real_repo)]


def test_malformed_spec_does_not_break_discovery(spec_root, real_repo):
    # Arrange — one broken spec must not hide the other 99. A broken spec
    # is the agent-list's problem to surface, not the GC's to crash on.
    _write_spec(spec_root, "good", str(real_repo))
    bad = spec_root / "broken"
    bad.mkdir()
    (bad / "spec.yaml").write_text("{[ this is not: valid yaml ::::\n")
    # Act
    found = discover_repos([spec_root])
    # Assert
    assert found == [str(real_repo)]


def test_spec_without_a_workdir_is_skipped(spec_root):
    # Arrange — a spec that declares no workdir declares no repo.
    _write_spec(spec_root, "workdirless", None)
    # Act
    found = spec_workdirs([spec_root])
    # Assert
    assert found == []


def test_missing_spec_root_yields_nothing(tmp_path):
    # Arrange — a fresh install has no agent tree at all.
    # Act
    found = discover_repos([tmp_path / "does-not-exist"])
    # Assert
    assert found == []


def test_discovery_is_sorted_and_deterministic(spec_root, tmp_path):
    # Arrange — two real repos declared in reverse-alphabetical spec order;
    # a pass must read the same twice.
    repos = []
    for name in ("zeta-repo", "alpha-repo"):
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(
            ["git", "-C", str(repo), "init", "-q", "-b", "develop"], check=True
        )
        _write_spec(spec_root, f"agent-{name}", str(repo))
        repos.append(str(repo))
    # Act
    found = discover_repos([spec_root])
    # Assert
    assert found == sorted(repos)
