"""Tests for CI-verdict owner resolution (sac #404).

feedback.pdf §3 + scitex-dev handoff (2026-06-17): resolve a repo → the
owning agent to deliver the verdict to, entirely from SAC'S OWN
agent-spec registry (ownership is sac's own data — every spec names its
target repo — so no external task store is read):

  1. PRIMARY  — sac's own agent specs: ``metadata.labels.project`` ↔ repo
     basename (sac-local, authoritative, no cross-package read).
  2. FALLBACK — PR body ``Owner:`` line (per-PR override).

Conventions: one assertion per test (STX-TQ007); AAA markers; no mocks
(STX-NM) — real YAML spec files under ``tmp_path``, injected via the
``agents_dir`` seam.
"""

from __future__ import annotations

from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

from pathlib import Path

from scitex_agent_container._lifecycle._ci_owner import resolve_owner, tracked_repos


def _write_spec(agents_dir: Path, agent_name: str, project: str) -> None:
    d = agents_dir / agent_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.yaml").write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "metadata:\n"
        "  labels:\n"
        f"    project: {project}\n"
        "spec:\n"
        "  runtime: tui\n")
    )


def test_agent_spec_label_project_resolves_owner(tmp_path: Path):
    # Arrange — an agent spec whose project label names the repo.
    agents = tmp_path / "agents"
    _write_spec(agents, "proj-scitex-dev", "scitex-dev")
    # Act
    owner = resolve_owner("ywatanabe1989/scitex-dev", agents_dir=agents)
    # Assert
    assert owner == "proj-scitex-dev"


def test_resolution_reads_only_specs_no_task_file(tmp_path: Path):
    # Arrange — an agent spec resolves the owner; a tasks.yaml sits in the
    # SAME dir with a CONFLICTING owner. If resolution touched a task file
    # it would surface the wrong name; it must ignore it entirely.
    agents = tmp_path / "agents"
    _write_spec(agents, "proj-from-spec", "scitex-dev")
    (tmp_path / "tasks.yaml").write_text(
        "tasks:\n  - repo: scitex-dev\n    agent: proj-from-tasks\n"
    )
    # Act — no tasks_path seam exists anymore; only agents_dir is read.
    owner = resolve_owner("ywatanabe1989/scitex-dev", agents_dir=agents)
    # Assert — the spec-registry answer, never the task file's.
    assert owner == "proj-from-spec"


def test_pr_body_owner_line_is_fallback_when_no_spec(tmp_path: Path):
    # Arrange — no matching spec; only the PR body carries the owner.
    agents = tmp_path / "agents"
    agents.mkdir()
    body = "## Summary\n\nOwner: proj-from-body\n\nmore text\n"
    # Act
    owner = resolve_owner("o/unmatched", agents_dir=agents, pr_body=body)
    # Assert
    assert owner == "proj-from-body"


def test_agent_spec_takes_precedence_over_pr_body(tmp_path: Path):
    # Arrange — both present; the spec (PRIMARY) must win over the PR body.
    agents = tmp_path / "agents"
    _write_spec(agents, "proj-from-spec", "scitex-dev")
    body = "Owner: proj-from-body\n"
    # Act
    owner = resolve_owner("scitex-dev", agents_dir=agents, pr_body=body)
    # Assert
    assert owner == "proj-from-spec"


def test_unknown_repo_resolves_to_none(tmp_path: Path):
    # Arrange — no spec matches and no PR-body override.
    agents = tmp_path / "agents"
    agents.mkdir()
    # Act
    owner = resolve_owner("o/nope", agents_dir=agents)
    # Assert
    assert owner is None


def test_tracked_repos_derives_owner_repo_from_spec_labels(tmp_path: Path):
    # Arrange — canonicalize seamed to identity: this test is about the
    # construction from spec labels, not about the GitHub lookup.
    agents = tmp_path / "agents"
    _write_spec(agents, "proj-scitex-dev", "scitex-dev")
    # Act
    repos = tracked_repos(
        agents_dir=agents, org="an-org", canonicalize=lambda repo: repo
    )
    # Assert
    assert repos == ["an-org/scitex-dev"]


def test_tracked_repos_empty_when_no_specs(tmp_path: Path):
    # Arrange
    agents = tmp_path / "agents"
    agents.mkdir()
    # Act
    repos = tracked_repos(
        agents_dir=agents, org="an-org", canonicalize=lambda repo: repo
    )
    # Assert
    assert repos == []


# --- canonical owner/name resolution -----------------------------------
# The constructed `<org>/<project>` is a GUESS about who owns the repo
# today. GitHub redirects path-addressed REST GETs after a transfer, so a
# stale guess keeps working and never reports itself — while every
# notification quotes an owner that may no longer exist, and search
# endpoints silently return nothing for it.


def test_tracked_repos_reports_the_name_github_returns_not_the_guess(
    tmp_path: Path,
):
    # Arrange — the constructed guess and the canonical answer differ,
    # which is exactly the transfer case that caused this.
    agents = tmp_path / "agents"
    _write_spec(agents, "proj-hub", "scitex-hub")
    # Act
    repos = tracked_repos(
        agents_dir=agents,
        org="old-owner",
        canonicalize=lambda repo: "new-org/scitex-hub",
    )
    # Assert
    assert repos == ["new-org/scitex-hub"]


def test_tracked_repos_collapses_two_projects_that_resolve_to_one_repo(
    tmp_path: Path,
):
    # Arrange — de-dup must happen AFTER resolution, or a renamed repo is
    # polled twice under two names.
    agents = tmp_path / "agents"
    _write_spec(agents, "proj-a", "old-name")
    _write_spec(agents, "proj-b", "new-name")
    # Act
    repos = tracked_repos(
        agents_dir=agents, org="an-org", canonicalize=lambda repo: "an-org/new-name"
    )
    # Assert
    assert repos == ["an-org/new-name"]


def test_canonical_lookup_falls_back_to_the_guess_when_gh_gives_nothing():
    # Arrange — a gh outage must degrade to the previous behaviour (a
    # guess), never to an empty poll list that silently watches nothing.
    from scitex_agent_container._lifecycle import _ci_owner as mod

    mod._CANONICAL_CACHE.clear()
    saved = mod._run_gh if hasattr(mod, "_run_gh") else None
    import scitex_agent_container._lifecycle._github_ci as ghmod

    real = ghmod._run_gh
    ghmod._run_gh = lambda args: ""
    try:
        # Act
        out = mod._canonical_name_with_owner("an-org/a-repo")
    finally:
        ghmod._run_gh = real
        mod._CANONICAL_CACHE.clear()
    # Assert
    assert out == "an-org/a-repo"


def test_canonical_lookup_is_cached_so_a_tick_costs_one_call_per_repo():
    # Arrange — without a cache the poll loop spends one gh call per repo
    # per tick, forever.
    from scitex_agent_container._lifecycle import _ci_owner as mod
    import scitex_agent_container._lifecycle._github_ci as ghmod

    mod._CANONICAL_CACHE.clear()
    calls: list = []
    real = ghmod._run_gh
    ghmod._run_gh = lambda args: calls.append(args) or "an-org/canonical"
    try:
        # Act
        mod._canonical_name_with_owner("an-org/a-repo")
        mod._canonical_name_with_owner("an-org/a-repo")
    finally:
        ghmod._run_gh = real
        mod._CANONICAL_CACHE.clear()
    # Assert
    assert len(calls) == 1
