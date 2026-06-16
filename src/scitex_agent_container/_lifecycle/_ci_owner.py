"""Resolve a repo → the owning agent for CI-verdict delivery (sac #404).

feedback.pdf §3 + scitex-dev handoff (2026-06-17): when sac polls a CI
verdict it must deliver it to the agent that owns the repo. Resolution
order (dev-confirmed — most authoritative + sac-local first):

  1. PRIMARY — sac's own agent specs ``<agents_dir>/*/spec.yaml``:
     ``metadata.labels.project`` matched against the repo basename. The
     returned owner is the agent's directory name (the canonical
     ``sac agents`` name that :func:`peer.post_turn` resolves).
  2. tasks.yaml — a task whose ``repo`` matches → its ``agent``/``assignee``.
  3. FALLBACK — a ``Owner: <agent>`` line in the PR body.

All reads are fail-soft (a malformed spec / tasks file contributes
nothing) so one bad row never blocks delivery to a resolvable owner.
``None`` means "no owner found" → the caller skips delivery for that PR.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_OWNER_LINE = re.compile(r"^\s*Owner:\s*(\S+)\s*$", re.MULTILINE)


def _repo_basename(repo: str) -> str:
    return repo.split("/")[-1].strip()


def _default_agents_dir() -> Path:
    return Path.home() / ".scitex" / "agent-container" / "agents"


def _default_tasks_path() -> Path:
    env = os.environ.get("SCITEX_TODO_TASKS")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".scitex" / "todo" / "tasks.yaml"


def _owner_from_agent_specs(basename: str, agents_dir: Path) -> str | None:
    if not agents_dir.is_dir():
        return None
    import yaml

    for spec in sorted(agents_dir.glob("*/spec.yaml")):
        try:
            doc = yaml.safe_load(spec.read_text()) or {}
            project = (doc.get("metadata") or {}).get("labels", {}).get("project", "")
        except Exception:  # stx-allow: fallback (one bad spec contributes nothing)
            continue
        if isinstance(project, str) and project.strip() == basename:
            return spec.parent.name
    return None


def _owner_from_tasks(basename: str, tasks_path: Path) -> str | None:
    if not tasks_path.is_file():
        return None
    import yaml

    try:
        doc = yaml.safe_load(tasks_path.read_text()) or {}
    except Exception:  # stx-allow: fallback (unreadable tasks store → no owner here)
        return None
    for task in doc.get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        repo = str(task.get("repo", "")).strip()
        if repo and _repo_basename(repo) == basename:
            owner = task.get("agent") or task.get("assignee")
            if isinstance(owner, str) and owner.strip():
                return owner.strip()
    return None


def _owner_from_pr_body(pr_body: str) -> str | None:
    m = _OWNER_LINE.search(pr_body)
    return m.group(1) if m else None


def resolve_owner(
    repo: str,
    *,
    pr_body: str | None = None,
    agents_dir: Path | None = None,
    tasks_path: Path | None = None,
) -> str | None:
    """Resolve ``repo`` → owning agent name, or ``None`` if unresolvable.

    See module docstring for the (spec → tasks.yaml → PR ``Owner:``)
    precedence. ``agents_dir`` / ``tasks_path`` are injection seams for
    tests; production callers leave them ``None`` to use the canonical
    host locations.
    """
    basename = _repo_basename(repo)
    if not basename:
        return None
    agents_dir = agents_dir if agents_dir is not None else _default_agents_dir()
    tasks_path = tasks_path if tasks_path is not None else _default_tasks_path()

    owner = _owner_from_agent_specs(basename, agents_dir)
    if owner:
        return owner
    owner = _owner_from_tasks(basename, tasks_path)
    if owner:
        return owner
    if pr_body:
        owner = _owner_from_pr_body(pr_body)
        if owner:
            return owner
    return None


def tracked_repos(*, agents_dir: Path | None = None, org: str | None = None) -> list:
    """Return the ``owner/repo`` strings the CI poller should watch.

    Derived from sac's own agent specs (the repos that have an owning
    agent): each spec's ``metadata.labels.project`` becomes
    ``<org>/<project>``. ``org`` defaults to ``$SAC_CI_POLL_ORG`` then
    ``ywatanabe1989`` (the SciTeX GitHub org). Sorted + de-duped; a
    project with no agent contributes nothing, so the poller only ever
    watches repos sac can actually deliver a verdict for.
    """
    agents_dir = agents_dir if agents_dir is not None else _default_agents_dir()
    resolved_org = org or os.environ.get("SAC_CI_POLL_ORG", "ywatanabe1989")
    if not agents_dir.is_dir():
        return []
    import yaml

    projects: set[str] = set()
    for spec in sorted(agents_dir.glob("*/spec.yaml")):
        try:
            doc = yaml.safe_load(spec.read_text()) or {}
            project = (doc.get("metadata") or {}).get("labels", {}).get("project", "")
        except Exception:  # stx-allow: fallback (one bad spec contributes nothing)
            continue
        if isinstance(project, str) and project.strip():
            projects.add(project.strip())
    return [f"{resolved_org}/{p}" for p in sorted(projects)]


__all__ = ["resolve_owner", "tracked_repos"]
