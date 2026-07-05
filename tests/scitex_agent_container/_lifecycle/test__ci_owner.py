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

import os
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._ci_owner import (
    ENV_CARD_STORE,
    ENV_CARD_STORE_LEGACY,
    _default_tasks_path,
    resolve_owner,
    tracked_repos,
)


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


# ---------------------------------------------------------------------------
# Card-store PATH resolution: generic sac var → legacy var → default.
# Real ``os.environ`` set/restore (NOT monkeypatch of any internal).
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_card_store_env():
    """Snapshot + restore the two card-store env vars around a test."""
    saved = {k: os.environ.get(k) for k in (ENV_CARD_STORE, ENV_CARD_STORE_LEGACY)}
    for k in (ENV_CARD_STORE, ENV_CARD_STORE_LEGACY):
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_generic_sac_env_var_is_read_first(tmp_path: Path, clean_card_store_env):
    # Arrange — BOTH vars set; the generic sac var must win.
    generic = tmp_path / "generic-cards.yaml"
    os.environ[ENV_CARD_STORE] = str(generic)
    os.environ[ENV_CARD_STORE_LEGACY] = str(tmp_path / "legacy.yaml")
    # Act
    path = _default_tasks_path()
    # Assert
    assert path == generic


def test_legacy_env_var_is_deprecated_fallback(tmp_path: Path, clean_card_store_env):
    # Arrange — only the legacy scitex-todo var set.
    legacy = tmp_path / "legacy-cards.yaml"
    os.environ[ENV_CARD_STORE_LEGACY] = str(legacy)
    # Act
    path = _default_tasks_path()
    # Assert — still honoured so old environments don't break.
    assert path == legacy


def test_default_path_when_no_env_var_set(clean_card_store_env):
    # Arrange — neither var set (fixture cleared both).
    # Act
    path = _default_tasks_path()
    # Assert — on-disk default under the user's ~/.scitex/todo.
    assert path == Path.home() / ".scitex" / "todo" / "tasks.yaml"


def test_generic_env_var_name_is_sac_namespaced():
    # Arrange
    name = ENV_CARD_STORE
    # Act
    observed = name
    # Assert — no scitex_todo hardcoding in the primary var name.
    assert observed == "SAC_CARD_STORE"
