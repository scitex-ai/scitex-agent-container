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

#: Cache sentinel. ``None`` is now a MEANINGFUL cached value ("GitHub says this
#: repo does not exist"), so a plain ``.get(repo)`` returning ``None`` can no
#: longer be read as "not cached" — that would re-probe every absent repo on
#: every tick and lose the whole saving.
_UNCACHED = object()

#: Substrings that mean GitHub ANSWERED and the answer was "no such repository".
#: Deliberately narrow. The asymmetry is the point: a false ABSENT silently
#: drops a real repo from the poll set and CI verdicts for it stop arriving with
#: nothing to notice; a false UNKNOWN merely keeps polling, which is today's
#: behaviour. So anything not clearly an absence is treated as unknown.
#: A BARE "not found" is deliberately NOT in this list. It would match
#: `gh: command not found` — a missing binary, which is the most UNKNOWN
#: situation there is — and turn it into "every repo is absent", emptying the
#: poll set on a host where gh simply is not installed. Each marker below has
#: to name a REPOSITORY.
_ABSENT_MARKERS = (
    "could not resolve to a repository",
    "no such repository",
    "404: not found",
)


def _says_no_such_repo(stderr: str) -> bool:
    """True only when gh's stderr states the repository does not exist."""
    low = (stderr or "").lower()
    return any(m in low for m in _ABSENT_MARKERS)


def _canonical_name_with_owner(repo: str) -> str | None:
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
    # NOTE the default. Plain ``.get(repo)`` returns None for a MISSING key,
    # which is indistinguishable from the cached value None ("absent repo") —
    # so without the sentinel default this returns None for every uncached
    # repo and never probes at all.
    cached = _CANONICAL_CACHE.get(repo, _UNCACHED)
    if cached is not _UNCACHED:
        return cached
    try:
        from ._github_ci import _run_gh_probe

        probe = _run_gh_probe(
            ["repo", "view", repo, "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
        )
    except Exception:  # stx-allow: fallback (gh missing → UNKNOWN, keep the constructed name)
        probe = None
    resolved = (probe.stdout if probe else "").strip()
    if "/" in resolved:
        out = resolved
    elif probe is not None and _says_no_such_repo(probe.stderr):
        # GitHub answered, and the answer was "there is no such repository".
        out = None
    else:
        # UNKNOWN — network, auth, rate limit, gh missing. Keep the constructed
        # guess, which is exactly the previous behaviour.
        out = repo
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
    # DROP the ones GitHub says do not exist. Measured 2026-08-20: 22 of the 94
    # names built from agent specs resolve to no repo in either org
    # (SAC_PLACEHOLDER_PROJECT, <PROJECT>, handyman-01..08, canary-resume-test,
    # …). Each cost one REST call per tick per host, forever, to be told 404 —
    # and the poller cannot deliver a verdict for a repo that does not exist, so
    # polling it was never doing anything but spending the shared budget.
    #
    # ``canonicalize`` returns None ONLY for a definite absence; an unknown
    # answer keeps the constructed guess, so a gh outage still degrades to
    # today's behaviour rather than to an empty poll list.
    return sorted(
        {
            name
            for name in (canonicalize(f"{resolved_org}/{p}") for p in projects)
            if name
        }
    )


__all__ = [
    "resolve_owner",
    "tracked_repos",
]
