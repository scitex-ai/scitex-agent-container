"""Resolve a repo → the owning agent for CI-verdict delivery (sac #404).

feedback.pdf §3 + scitex-dev handoff (2026-06-17): when sac polls a CI
verdict it must deliver it to the agent that owns the repo. Ownership is
SAC'S OWN DATA — every agent spec already names its target repo — so sac
answers "which agent owns this failing repo" entirely from its OWN
agent-spec registry, with no read of any external task store.

Resolution order (sac-local first):

  1. PRIMARY — sac's own agent specs ``<agents_dir>/*/spec.yaml``:
     ``metadata.labels.project`` matched against the repo basename. The
     returned owner is the agent's directory name (the canonical
     ``sac agents`` name that :func:`peer.post_turn` resolves).
  2. FALLBACK — a ``Owner: <agent>`` line in the PR body (a per-PR
     override for a repo that has no owning agent spec).

All reads are fail-soft (a malformed spec contributes nothing) so one
bad row never blocks delivery to a resolvable owner. ``None`` means "no
owner found" → the caller skips delivery for that PR.
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


def _owner_from_pr_body(pr_body: str) -> str | None:
    m = _OWNER_LINE.search(pr_body)
    return m.group(1) if m else None


def resolve_owner(
    repo: str,
    *,
    pr_body: str | None = None,
    agents_dir: Path | None = None,
) -> str | None:
    """Resolve ``repo`` → owning agent name, or ``None`` if unresolvable.

    See module docstring for the (agent-spec → PR ``Owner:``) precedence.
    Resolution is entirely from sac's own agent-spec registry; no external
    task store is read. ``agents_dir`` is an injection seam for tests;
    production callers leave it ``None`` to use the canonical host
    location.
    """
    basename = _repo_basename(repo)
    if not basename:
        return None
    agents_dir = agents_dir if agents_dir is not None else _default_agents_dir()

    owner = _owner_from_agent_specs(basename, agents_dir)
    if owner:
        return owner
    if pr_body:
        owner = _owner_from_pr_body(pr_body)
        if owner:
            return owner
    return None


#: Process-lifetime cache of constructed name → canonical ``owner/repo``.
#: A transfer or rename is rare and the poller restarts often, so a
#: per-process cache is enough; it keeps the poll tick from spending one
#: ``gh`` call per repo per tick.
_CANONICAL_CACHE: dict = {}


def _canonical_name_with_owner(repo: str) -> str:
    """Return GitHub's canonical ``owner/name`` for ``repo``.

    A constructed ``<org>/<project>`` string is a GUESS about who owns the
    repo today. GitHub redirects path-addressed REST GETs after a transfer,
    so the guess keeps working and nothing ever reports that it is stale —
    while every notification quotes an owner that may no longer exist.
    Measured 2026-08-16: the ring named a repo by a pre-transfer owner for
    long enough to cost a whole investigation into whether two repos
    existed. Search endpoints do NOT follow the redirect, so the stale name
    also silently returns zero results there.

    Fail-soft: any failure returns ``repo`` unchanged, which is exactly the
    previous behaviour, so a gh outage degrades to a guess rather than to
    an empty poll list.
    """
    cached = _CANONICAL_CACHE.get(repo)
    if cached is not None:
        return cached
    try:
        from ._github_ci import _run_gh

        raw = _run_gh(
            ["repo", "view", repo, "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
        )
    except Exception:  # stx-allow: fallback (gh missing → keep the constructed name)
        raw = ""
    resolved = (raw or "").strip()
    out = resolved if "/" in resolved else repo
    _CANONICAL_CACHE[repo] = out
    return out


def tracked_repos(
    *,
    agents_dir: Path | None = None,
    org: str | None = None,
    canonicalize=None,
) -> list:
    """Return the ``owner/repo`` strings the CI poller should watch.

    Derived from sac's own agent specs (the repos that have an owning
    agent): each spec's ``metadata.labels.project`` becomes
    ``<org>/<project>``, which is then resolved to the name GitHub
    actually reports. ``org`` seeds that lookup and defaults to
    ``$SAC_CI_POLL_ORG``; the constructed string is a starting guess, not
    the answer. Sorted + de-duped AFTER resolution, so two projects that
    resolve to one repo collapse. A project with no agent contributes
    nothing, so the poller only ever watches repos sac can deliver a
    verdict for.

    ``canonicalize`` is an injection seam; production callers leave it
    ``None``.
    """
    agents_dir = agents_dir if agents_dir is not None else _default_agents_dir()
    resolved_org = org or os.environ.get("SAC_CI_POLL_ORG", "ywatanabe1989")
    canonicalize = canonicalize or _canonical_name_with_owner
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
    return sorted({canonicalize(f"{resolved_org}/{p}") for p in projects})


__all__ = [
    "resolve_owner",
    "tracked_repos",
]
