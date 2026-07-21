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
    # Arrange
    agents = tmp_path / "agents"
    _write_spec(agents, "proj-scitex-dev", "scitex-dev")
    # Act
    repos = tracked_repos(agents_dir=agents, org="ywatanabe1989")
    # Assert
    assert repos == ["ywatanabe1989/scitex-dev"]


def test_tracked_repos_empty_when_no_specs(tmp_path: Path):
    # Arrange
    agents = tmp_path / "agents"
    agents.mkdir()
    # Act
    repos = tracked_repos(agents_dir=agents, org="ywatanabe1989")
    # Assert
    assert repos == []
