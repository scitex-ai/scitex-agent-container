"""Tests for CI-verdict owner resolution (sac #404).

feedback.pdf §3 + scitex-dev handoff (2026-06-17): resolve a repo → the
owning agent to deliver the verdict to, in order:

  1. PRIMARY  — sac's own agent specs: ``metadata.labels.project`` ↔ repo
     basename (sac-local, authoritative, no cross-package read).
  2. tasks.yaml — task ``repo`` field → owning ``agent``.
  3. FALLBACK — PR body ``Owner:`` line.

Conventions: one assertion per test (STX-TQ007); AAA markers; no mocks
(STX-NM) — real YAML files under ``tmp_path``, injected via the
``agents_dir`` / ``tasks_path`` seams.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._lifecycle._ci_owner import resolve_owner, tracked_repos


def _write_spec(agents_dir: Path, agent_name: str, project: str) -> None:
    d = agents_dir / agent_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.yaml").write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "metadata:\n"
        "  labels:\n"
        f"    project: {project}\n"
        "spec:\n"
        "  runtime: tui\n"
    )


def test_agent_spec_label_project_resolves_owner(tmp_path: Path):
    # Arrange
    agents = tmp_path / "agents"
    _write_spec(agents, "proj-scitex-dev", "scitex-dev")
    # Act
    owner = resolve_owner("ywatanabe1989/scitex-dev", agents_dir=agents)
    # Assert
    assert owner == "proj-scitex-dev"


def test_tasks_yaml_repo_resolves_owner_when_no_spec(tmp_path: Path):
    # Arrange — empty agents dir; owner only in tasks.yaml.
    agents = tmp_path / "agents"
    agents.mkdir()
    tasks = tmp_path / "tasks.yaml"
    tasks.write_text("tasks:\n  - repo: scitex-dev\n    agent: proj-from-tasks\n")
    # Act
    owner = resolve_owner(
        "ywatanabe1989/scitex-dev", agents_dir=agents, tasks_path=tasks
    )
    # Assert
    assert owner == "proj-from-tasks"


def test_pr_body_owner_line_is_last_fallback(tmp_path: Path):
    # Arrange — nothing in specs or tasks; only the PR body carries it.
    agents = tmp_path / "agents"
    agents.mkdir()
    body = "## Summary\n\nOwner: proj-from-body\n\nmore text\n"
    # Act
    owner = resolve_owner("o/unmatched", agents_dir=agents, pr_body=body)
    # Assert
    assert owner == "proj-from-body"


def test_agent_spec_takes_precedence_over_tasks(tmp_path: Path):
    # Arrange — both present; the spec (PRIMARY) must win.
    agents = tmp_path / "agents"
    _write_spec(agents, "proj-from-spec", "scitex-dev")
    tasks = tmp_path / "tasks.yaml"
    tasks.write_text("tasks:\n  - repo: scitex-dev\n    agent: proj-from-tasks\n")
    # Act
    owner = resolve_owner("scitex-dev", agents_dir=agents, tasks_path=tasks)
    # Assert
    assert owner == "proj-from-spec"


def test_unknown_repo_resolves_to_none(tmp_path: Path):
    # Arrange
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
