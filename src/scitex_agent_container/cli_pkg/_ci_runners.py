"""Which RUNNER executed each job — the compliance read a status word hides.

A green check is not proof of compliance. Measured 2026-08-11: scitex-dev
PR #572 showed ``import-smoke  pass  1m9s`` while the jobs API reported
``runner_name=spartan-cpu-org-01``. The run had been queued BEFORE the repo's
``CI_RUNS_ON`` was repointed off Spartan, so it executed on exactly the
hardware the change existed to abandon — and every human-visible signal said
green. Re-reading the variable would also have said green, because the
variable HAD been changed; only the job's actual ``runner_name`` disagreed.

So this module reads ``runner_name`` rather than status, for EVERY job of a
run (not just failing ones — the failing ones were never the risk), and fails
loud when a banned runner appears.

It also reports AMBIGUOUS selectors. ``scitex-ci`` is carried by Spartan
runners as well as compliant ones, so a workflow selecting it can still land
on Spartan; that is a trap which happens to be harmless only while no banned
runner is registered. Reported as a warning, not a violation: the job in hand
did run somewhere compliant, but the selector gives no guarantee it will next
time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from ._ci_why import CIWhyError, GhRunner, _repo_args, _RUN_ID_IN_LINK, run_gh

__all__ = [
    "BANNED_RUNNER_SUBSTRINGS",
    "AMBIGUOUS_LABELS",
    "JobRunner",
    "RunRunners",
    "audit",
    "render_text",
    "resolve_all_run_ids",
]

# Substring match on the runner NAME. Names are the durable identity here:
# a banned host can be relabelled, but it is still the banned host.
BANNED_RUNNER_SUBSTRINGS = ("spartan",)

# Labels carried by BOTH banned and compliant runners, so selecting one is
# not a guarantee of anything.
AMBIGUOUS_LABELS = ("scitex-ci",)


def is_banned(runner_name: Optional[str]) -> bool:
    """True when this runner name identifies banned hardware."""
    name = (runner_name or "").lower()
    return any(bad in name for bad in BANNED_RUNNER_SUBSTRINGS)


def ambiguous_labels(labels: list[str]) -> list[str]:
    """The selector labels that can also match banned hardware."""
    lowered = {str(x).lower() for x in labels or []}
    return [lab for lab in AMBIGUOUS_LABELS if lab in lowered]


@dataclass
class JobRunner:
    """One job, and the machine that actually ran it."""

    job: str
    status: str
    conclusion: Optional[str]
    runner_name: Optional[str]
    runner_group: Optional[str]
    labels: list[str] = field(default_factory=list)
    url: Optional[str] = None

    @property
    def banned(self) -> bool:
        return is_banned(self.runner_name)

    @property
    def ambiguous(self) -> list[str]:
        return ambiguous_labels(self.labels)

    def to_dict(self) -> dict:
        return {
            "job": self.job,
            "status": self.status,
            "conclusion": self.conclusion,
            "runner_name": self.runner_name,
            "runner_group": self.runner_group,
            "labels": self.labels,
            "banned": self.banned,
            "ambiguous_labels": self.ambiguous,
            "url": self.url,
        }


@dataclass
class RunRunners:
    """Every job of one run, with the runner each landed on."""

    run_id: str
    jobs: list[JobRunner] = field(default_factory=list)

    @property
    def violations(self) -> list[JobRunner]:
        return [j for j in self.jobs if j.banned]

    @property
    def warnings(self) -> list[JobRunner]:
        return [j for j in self.jobs if j.ambiguous and not j.banned]

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "jobs": [j.to_dict() for j in self.jobs],
            "violations": [j.job for j in self.violations],
            "warnings": [j.job for j in self.warnings],
        }


def _jobs_path(run_id: str, repo: Optional[str]) -> str:
    # gh substitutes {owner}/{repo} from the cwd's repo when none is given.
    owner_repo = repo if repo else "{owner}/{repo}"
    return f"repos/{owner_repo}/actions/runs/{run_id}/jobs"


def run_job_runners(
    run_id: str, *, run_gh: GhRunner = run_gh, repo: Optional[str] = None
) -> RunRunners:
    """Read every job of ``run_id`` and the runner it executed on."""
    raw = run_gh(["api", "--paginate", _jobs_path(run_id, repo)])
    try:
        payload = json.loads(raw or "{}")
    except ValueError as exc:
        raise CIWhyError(f"could not parse jobs JSON for run {run_id}") from exc

    jobs_raw = payload.get("jobs") if isinstance(payload, dict) else None
    if jobs_raw is None:
        raise CIWhyError(f"no jobs found for run {run_id}")

    jobs = [
        JobRunner(
            job=str(j.get("name", "?")),
            status=str(j.get("status", "?")),
            conclusion=j.get("conclusion"),
            runner_name=j.get("runner_name"),
            runner_group=j.get("runner_group_name"),
            labels=list(j.get("labels") or []),
            url=j.get("html_url"),
        )
        for j in jobs_raw
    ]
    return RunRunners(run_id=str(run_id), jobs=jobs)


def _pr_all_run_ids(pr: str, *, run_gh: GhRunner, repo: Optional[str]) -> list[str]:
    """EVERY run behind a PR's checks — passing ones included.

    Deliberately not ``_ci_why._pr_failing_run_ids``: a compliance violation
    is most likely to be hiding behind a check that PASSED.
    """
    raw = run_gh(["pr", "checks", pr, *_repo_args(repo), "--json", "name,state,link"])
    try:
        checks = json.loads(raw or "[]")
    except ValueError as exc:
        raise CIWhyError(f"could not parse gh pr checks JSON for PR {pr}") from exc
    run_ids: list[str] = []
    for c in checks or []:
        m = _RUN_ID_IN_LINK.search(str(c.get("link", "")))
        if m and m.group(1) not in run_ids:
            run_ids.append(m.group(1))
    if not run_ids:
        raise CIWhyError(f"no workflow runs found behind PR {pr}")
    return run_ids


def resolve_all_run_ids(
    target: Optional[str], *, run_gh: GhRunner = run_gh, repo: Optional[str] = None
) -> list[str]:
    """PR number / run id / branch -> ALL relevant run ids (not just red)."""
    from ._ci_why import _RUN_ID_MIN, _branch_latest_run_id, _current_branch

    target = (target or "").strip()
    if not target:
        return _branch_latest_run_id(_current_branch(), run_gh=run_gh, repo=repo)
    if target.isdigit():
        if int(target) >= _RUN_ID_MIN:
            return [target]
        return _pr_all_run_ids(target, run_gh=run_gh, repo=repo)
    return _branch_latest_run_id(target, run_gh=run_gh, repo=repo)


def audit(
    target: Optional[str], *, run_gh: GhRunner = run_gh, repo: Optional[str] = None
) -> list[RunRunners]:
    """Resolve ``target`` and report the runner behind each of its jobs."""
    ids = resolve_all_run_ids(target, run_gh=run_gh, repo=repo)
    return [run_job_runners(rid, run_gh=run_gh, repo=repo) for rid in ids]


def render_text(run: RunRunners) -> str:
    """One line per job; violations and warnings called out explicitly."""
    lines = []
    for j in run.jobs:
        mark = "BANNED " if j.banned else "        "
        where = j.runner_name or "<unassigned>"
        grp = f" [{j.runner_group}]" if j.runner_group else ""
        concl = j.conclusion or j.status
        lines.append(f"  {mark}{concl:<12} {where}{grp}  {j.job}")
        for lab in j.ambiguous:
            lines.append(
                f"          warning: selector '{lab}' also matches banned runners"
            )
    return "\n".join(lines)
